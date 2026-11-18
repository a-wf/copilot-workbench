#!/usr/bin/env python3
"""Opt-in real-CLI integration against a loopback-only fake model.

Run with COPILOT_TEST_OFFLINE_INTEGRATION=1 plus absolute
COPILOT_TEST_BINARY / COPILOT_TEST_RTK_BINARY paths. Never uses auth, hosted
models, user HOME/config or network services. The only server is 127.0.0.1.
Ordinary CI skips this test when the explicitly selected CLIs are absent.
"""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_copilot_session_report as fixture


class LocalModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self.send_json(200, {"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
        else:
            self.send_json(404, {"error": "local fixture route not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        tools = body.get("tools") or []
        results = [m.get("content") for m in body.get("messages", []) if m.get("role") == "tool"]
        self.server.results.extend(results)
        bash = next((t["function"] for t in tools if t.get("function", {}).get("name") == "bash"), None)
        if not results and bash:
            schema = bash.get("parameters", {})
            args = {"command": "cargo test"}
            for name in schema.get("required", []):
                if name in args:
                    continue
                p = schema.get("properties", {}).get(name, {})
                if p.get("enum"):
                    args[name] = "sync" if "sync" in p["enum"] else p["enum"][0]
                elif p.get("type") in ("number", "integer"):
                    args[name] = 30
                elif p.get("type") == "boolean":
                    args[name] = False
                else:
                    args[name] = "synthetic offline test"
            args.setdefault("description", "synthetic offline test")
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_" + uuid.uuid4().hex,
                     "type": "function", "function": {"name": "bash", "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
            self.server.issued += 1
        else:
            delta = {"role": "assistant", "content": "OFFLINE_DONE"}
            finish = "stop"
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        cid = "chatcmpl-" + uuid.uuid4().hex
        common = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                  "model": "mock-model"}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                dict(common, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                dict(common, choices=[{"index": 0, "delta": {}, "finish_reason": finish}]),
                dict(common, choices=[], usage=usage),
            ]
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            message = dict(delta)
            for call in message.get("tool_calls", []):
                call.pop("index", None)
            self.send_json(200, dict(common, object="chat.completion",
                choices=[{"index": 0, "message": message, "finish_reason": finish}], usage=usage))


@unittest.skipUnless(os.environ.get("COPILOT_TEST_OFFLINE_INTEGRATION") == "1",
                     "requires explicitly selected real CLIs; loopback fake-model integration")
class TestRealOfflineRTK(fixture.ReportHome):
    def test_real_plugin_executes_rewrite_permissions_fail_closed_and_reports_actual_session(self):
        copilot = Path(os.environ["COPILOT_TEST_BINARY"]).resolve()
        rtk = Path(os.environ["COPILOT_TEST_RTK_BINARY"]).resolve()
        self.assertTrue(copilot.is_absolute() and copilot.is_file())
        self.assertTrue(rtk.is_absolute() and rtk.is_file())
        self.rtk.unlink()
        self.rtk.symlink_to(rtk)
        self.env.update({"PATH": "%s:/usr/local/bin:/usr/bin:/bin" % self.bin,
            "COPILOT_OFFLINE": "true", "COPILOT_AUTO_UPDATE": "false",
            "COPILOT_PLUGIN_DIR_ONLY": "true", "COPILOT_PROVIDER_TYPE": "openai",
            "COPILOT_MODEL": "mock-model", "COPILOT_DISABLE_DESKTOP_NOTIFICATIONS": "1"})
        version = subprocess.run([str(copilot), "--version"], env=self.env, text=True,
                                 capture_output=True, timeout=20)
        self.assertIn("1.0.114", version.stdout, "integration fixture verified for Copilot 1.0.114 only")
        rv = subprocess.run([str(rtk), "--version"], env=self.env, text=True,
                            capture_output=True, timeout=20)
        self.assertEqual(rv.stdout.strip(), "rtk 1.2.0")
        subprocess.run(["git", "init", "-q", str(self.cwd)], env=self.env, check=True)
        subprocess.run(["git", "-C", str(self.cwd), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-q",
                        "--allow-empty", "-m", "offline fixture"], env=self.env, check=True)
        # If loaded, this unresolved extension breaks the RTK pipeline.
        # No trust store exists; it must remain inert, not be auto-trusted.
        fixture.write(self.cwd / ".rtk/pipeline.toml",
                      'schema_version = 1\n[pipeline]\nextends = ["test-untrusted-must-not-load"]\n')
        fixture.write(self.bin / "cargo", "#!%s\n" % os.sys.executable +
            "import os,pathlib\n"
            "pathlib.Path(os.environ['HOME'],'cargo-executed').write_text('yes')\n"
            "print('running 1 test\\ntest fixture ... ok\\n\\ntest result: ok. 1 passed; 0 failed')\n", True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModel)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.env["COPILOT_PROVIDER_BASE_URL"] = "http://127.0.0.1:%s/v1" % server.server_port
        for mode, allowed, denied, missing, should_execute in [
            ("normal", "shell(cargo:*)", None, False, True),
            ("rtk", "shell(rtk:*)", None, False, True),
            ("rtk", "shell(cargo:*)", "shell(rtk:*)", False, False),
            ("rtk", "shell(rtk:*)", None, True, False),
        ]:
            with self.subTest(mode=mode, deny=denied, missing=missing):
                marker = self.home / "cargo-executed"
                marker.unlink(missing_ok=True)
                server.results, server.issued = [], 0
                otel = self.home / ("integration-" + uuid.uuid4().hex + ".jsonl")
                run_id = self.start(mode, otel=otel)
                rdir = self.path(run_id)
                env = dict(self.env, COPILOT_OTEL_ENABLED="true", COPILOT_OTEL_EXPORTER_TYPE="file",
                           COPILOT_OTEL_FILE_EXPORTER_PATH=str(otel),
                           COPILOT_S_RUN_DIR=str(rdir),
                           COPILOT_S_RTK_BIN=str(self.home / "missing-rtk") if missing else str(rtk))
                env.update(self.mod.rtk_env_for(str(rdir)))
                args = [str(copilot), "-p", "Run the single synthetic fixture command.",
                        "--model", "mock-model", "--disable-builtin-mcps", "--disable-client-telemetry",
                        "--available-tools=bash", "--allow-tool=" + allowed,
                        "--usage-output-file", str(rdir / "raw/copilot-usage.json"),
                        "--log-dir", str(rdir / "raw/copilot-logs"), "--output-format", "json"]
                if mode == "rtk":
                    args += ["--plugin-dir", str(fixture.PLUGIN)]
                if denied:
                    args += ["--deny-tool=" + denied]
                proc = subprocess.run(args, cwd=self.cwd, env=env, text=True,
                                      capture_output=True, timeout=100)
                self.assertEqual(proc.returncode, 0, proc.stderr[-4000:] + proc.stdout[-2000:])
                self.assertEqual(server.issued, 1, proc.stdout[-2000:] + str(server.results))
                self.assertTrue(server.results, "real CLI must return a tool result to the local model")
                self.assertEqual(marker.exists(), should_execute, str(server.results))
                rec = self.finish(run_id, sid=None)
                self.assertEqual(rec["session"]["status"], "confirmed", json.dumps(rec))
                self.assertTrue((self.states / rec["session"]["id"]).is_dir())
                self.assertNotIn("Run the single synthetic fixture", json.dumps(rec))
                if mode == "rtk":
                    events = [json.loads(s) for s in (rdir / "hook-events.jsonl").read_text().splitlines()]
                    self.assertEqual({e["sid"] for e in events}, {rec["session"]["id"]})
                    if missing:
                        self.assertEqual(rec["rtk"]["hook"]["decisions"]["denied"], 1)
                    else:
                        self.assertEqual(rec["rtk"]["hook"]["decisions"]["rewritten"], 1)
                    if should_execute:
                        self.assertEqual(rec["rtk"]["stats"]["availability"], "available")
                        self.assertEqual(rec["rtk"]["stats"]["total_commands"], 1)
                self.assertFalse(any(self.home.rglob("trusted_filters.json")))


if __name__ == "__main__":
    unittest.main()
