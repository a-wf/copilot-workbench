#!/usr/bin/env python3
"""Content-free hook evidence, argv boundaries, and installed asset behavior."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unittest
from unittest import mock

import test_copilot_session_report as fixture


class TestRTKHook(fixture.ReportHome):
    def hook(self, command, *, tool="Bash", raw=None):
        run_dir = self.home / "hook-run"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        env = dict(self.env, COPILOT_S_RUN_DIR=str(run_dir), COPILOT_S_RTK_BIN=str(self.rtk))
        payload = {"session_id": "session-a", "tool_name": tool,
                   "tool_input": {"command": command, "description": "retained", "timeout": 30}}
        proc = subprocess.run([sys.executable, str(fixture.HOOK)], env=env, cwd=self.cwd,
                              input=raw if raw is not None else json.dumps(payload),
                              text=True, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = run_dir / "hook-events.jsonl"
        self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)
        event = json.loads(events.read_text().splitlines()[-1])
        self.assertEqual(set(event), {"v", "ts", "sid", "decision", "reason"})
        self.assertNotIn(command, events.read_text()) if command else None
        return json.loads(proc.stdout) if proc.stdout else None, event

    def test_exact_rewrite_retains_fields_no_permission_grant_and_content_free_evidence(self):
        for command in ("cargo test --workspace", "pytest tests/test_unit.py", "npm run build"):
            with self.subTest(command=command):
                result, event = self.hook(command)
                out = result["hookSpecificOutput"]
                self.assertEqual(out["updatedInput"], {"command": "rtk " + command,
                    "description": "retained", "timeout": 30})
                self.assertNotIn("permissionDecision", out)
                self.assertEqual(event["decision"], "rewritten")
                self.assertEqual(event["sid"], "session-a")
        for mode in ("empty", "nonexact"):
            with self.subTest(reply=mode):
                self.env["TEST_RTK_REPLY"] = mode
                result, event = self.hook("cargo test")
                self.assertIsNone(result)
                self.assertEqual(event["decision"], "unchanged")

    def test_review_tools_shell_syntax_env_assignments_and_other_tools_unchanged(self):
        counter = fixture.write(self.home / "rtk-invocations", "0")
        self.env.update(TEST_RTK_REPLY="rewrite-all", TEST_RTK_HOOK_COUNT=str(counter))
        # Positive control: this fixture WILL rewrite anything it receives.
        result, _ = self.hook("cargo test")
        self.assertEqual(result["hookSpecificOutput"]["updatedInput"]["command"], "rtk cargo test")
        self.assertEqual(counter.read_text(), "1")
        counter.write_text("0")
        excluded = {
            "not-allowlisted": ("git diff", "diff a b", "grep pattern file", "cat file", "echo hello",
                                "env cargo test", "rtk cargo test", "npm install", "npm run deploy",
                                "go run main.go", "cargo run", "cargo install fixture"),
            "not-plain-program": ("/usr/bin/cargo test", "ENV=1 cargo test"),
            "shell-syntax": ("cargo test; echo x", "cargo test && echo x", "cargo test | cat",
                             "cargo test > output", "cargo test $(echo x)", "cargo test `echo x`",
                             "cargo test $SECRET", "cargo test *.rs", 'cargo test "name"',
                             "cargo test\ntrue", "cargo test # comment", "cargo test \u0000",
                             "cargo\ttest", "cargo test {a,b}", "cargo test ~"),
        }
        for reason, commands in excluded.items():
            for command in commands:
                with self.subTest(command=command):
                    result, event = self.hook(command)
                    self.assertIsNone(result)
                    self.assertEqual(event["decision"], "unchanged")
                    self.assertEqual(event["reason"], reason)
                    self.assertEqual(counter.read_text(), "0", "classifier must reject BEFORE invoking RTK")
        result, event = self.hook("cargo test", tool="Read")
        self.assertIsNone(result)
        self.assertEqual(event["decision"], "unchanged")
        self.assertEqual(event["reason"], "not-bash")
        self.assertEqual(counter.read_text(), "0")

    def test_missing_failed_and_unparseable_rtk_deny_never_execute(self):
        for mode in ("error", "malformed", "array", "bad-command", "missing"):
            with self.subTest(mode=mode):
                self.env["TEST_RTK_REPLY"] = mode
                if mode == "missing":
                    self.rtk.unlink()
                result, event = self.hook("cargo test")
                self.assertEqual(result["hookSpecificOutput"]["permissionDecision"] if result else None, "deny")
                self.assertEqual(event["decision"], "denied")
                self.assertNotIn("updatedInput", result["hookSpecificOutput"])

    def test_upstream_rtk_permission_decisions_fail_closed_without_approval(self):
        for mode in ("decision", "decision-deny"):
            with self.subTest(reply=mode):
                self.env["TEST_RTK_REPLY"] = mode
                result, event = self.hook("cargo test")
                self.assertEqual(result["hookSpecificOutput"].get("permissionDecision") if result else None,
                                 "deny", "Never discard an upstream permission decision and run unfiltered")
                self.assertNotIn("updatedInput", result["hookSpecificOutput"])
                self.assertEqual(event["decision"], "denied")

    def test_malformed_and_oversized_payload_are_not_executed_or_logged(self):
        for payload in ("{", "[]", "x" * (1024 * 1024 + 1)):
            with self.subTest(size=len(payload)):
                result, event = self.hook("", raw=payload)
                self.assertIsNone(result)
                self.assertEqual(event["decision"], "unchanged")
                self.assertIsNone(event["sid"])


FAKE_COPILOT = r"""
import json, os, pathlib, sys
args = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
(home / "captured.json").write_text(json.dumps({"args": args,
    "env": {k: v for k, v in os.environ.items()
            if k.startswith(("RTK_", "COPILOT_OTEL_", "COPILOT_S_"))}}))
sid = "wrapper-session"
for arg in args:
    if arg.startswith("--resume="):
        sid = arg.split("=", 1)[1]
directory = home / ".copilot/session-state" / sid
directory.mkdir(parents=True, exist_ok=True)
workspace = directory / "workspace.yaml"
if not workspace.exists():
    workspace.write_text("cwd: " + os.getcwd() + "\nname: Fixture\n")
path = pathlib.Path(os.environ["COPILOT_OTEL_FILE_EXPORTER_PATH"])
path.write_text(json.dumps({"type": "span", "name": "chat gpt-6-luna",
    "traceId": "actual-trace", "spanId": "actual-span", "attributes": {
        "gen_ai.conversation.id": sid, "gen_ai.response.model": "gpt-6-luna",
        "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 1}}) + "\n")
if "--usage-output-file" in args:
    index = args.index("--usage-output-file")
    if "--" not in args or index < args.index("--"):
        pathlib.Path(args[index + 1]).write_text('{"totalUserRequests":1}')
sys.exit(int(os.environ.get("TEST_COPILOT_EXIT", "0")))
"""


class TestSessionReportingLauncher(fixture.ReportHome):
    def setUp(self):
        super().setUp()
        fixture.write(self.bin / "copilot", "#!%s\n%s" % (sys.executable, FAKE_COPILOT), True)
        source = fixture.LAUNCHER.read_text()
        self.assertTrue(source.rstrip().endswith('main "$@"'))
        self.functions = fixture.write(self.home / "launcher-functions.sh",
                                       source.rsplit('main "$@"', 1)[0])
        self.env.update({"COPILOT_SESSION_REPORT_HELPER": str(fixture.HELPER),
                         "COPILOT_S_RTK_PLUGIN_DIR": str(fixture.PLUGIN)})

    def launch(self, *args, expected=0, full_main=True, resume=""):
        # Stub only interactive UI/task-report seams. Real main option parsing,
        # RTK preflights, start/run/finalize, argv/env and session detection run.
        script = r'''
source "$1"; shift
prompt_read() { printf -v "$1" '%s' ""; }
flush_stdin() { :; }
resolve_task_id() { echo TEST-1; }
ensure_task_install_marker() { :; }
post_session() { finalize_run_report; }
list_sessions() { return 1; }
empty_session_menu() { run_copilot "" "$@"; }
'''
        if full_main:
            script += 'main "$@"\n'
        else:
            script += 'setup_otel; run_copilot "$TEST_RESUME" "$@"\n'
        proc = subprocess.run(["/bin/bash", "-c", script, "launcher-test", str(self.functions), *args],
                              env=dict(self.env, TEST_RESUME=resume), cwd=self.cwd,
                              text=True, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
        return proc

    def captured(self):
        return json.loads((self.home / "captured.json").read_text())

    def records(self):
        return [json.loads(p.read_text()) for p in
                (self.home / ".copilot/session-reports/runs").glob("*/run.json")]

    def test_normal_native_usage_auto_balance_unique_watcher_paths_and_exit_state(self):
        self.launch("--model=ignored", "--auto-tier", "ignored", "--name", "Fixture")
        one = self.captured()
        self.assertEqual(one["args"][:2], ["--name", "Fixture"])
        self.assertEqual(one["args"][2:6], ["--model", "auto", "--auto-tier", "balance"])
        self.assertEqual(one["args"].count("--usage-output-file"), 1)
        record = self.records()[0]
        self.assertEqual(record["mode"], "normal")
        self.assertEqual(record["session"]["id"], "wrapper-session")
        self.assertEqual(record["task_outcome"]["value"], "unknown")
        self.assertEqual(record["native_usage"]["totalUserRequests"], 1)
        self.assertNotIn("--plugin-dir", one["args"])
        path = one["env"]["COPILOT_OTEL_FILE_EXPORTER_PATH"]
        self.assertRegex(Path(path).name, r"^copilot-otel-\d{8}-\d{6}-[a-f0-9-]{36}\.jsonl$")
        self.assertIn(path, [str(p) for p in Path(path).parent.glob("copilot-otel-*.jsonl")])
        self.env["TEST_COPILOT_EXIT"] = "7"
        self.launch("--name", "Fixture")
        self.assertNotEqual(self.captured()["env"]["COPILOT_OTEL_FILE_EXPORTER_PATH"], path)
        self.assertEqual({r["state"] for r in self.records()}, {"completed", "copilot-error"})

    def test_explicit_usage_file_preserved_without_duplicate_and_not_read(self):
        caller = self.home / "caller-usage.json"
        for args in (["--usage-output-file", str(caller)], ["--usage-output-file=" + str(caller)]):
            with self.subTest(args=args):
                self.launch("--name", "Fixture", *args)
                argv = self.captured()["args"]
                self.assertEqual(sum(a.startswith("--usage-output-file") for a in argv), 1)
                for arg in args:
                    self.assertIn(arg, argv)
                self.assertTrue(all(r["capabilities"]["native_usage_file"] == "caller"
                                    and r["native_usage"]["availability"] == "unavailable"
                                    for r in self.records()))

    def test_rtk_is_per_launch_isolated_not_broadly_approved_and_resume_preserved(self):
        self.env.update(RTK_DB_PATH=str(self.home / "user-history.db"),
                        RTK_FILTER="PRIVATE_UNTRUSTED_FILTER", RTK_CUSTOM="do-not-forward")
        self.launch("--rtk", "--name", "Fixture")
        captured = self.captured()
        self.assertNotIn("--rtk", captured["args"])
        self.assertEqual(captured["args"][-2:], ["--plugin-dir", str(fixture.PLUGIN)])
        self.assertFalse(any(a.startswith(("--allow", "--yolo")) for a in captured["args"]))
        env = captured["env"]
        self.assertNotIn("RTK_FILTER", env)
        self.assertNotIn("RTK_CUSTOM", env)
        for key in ("RTK_DB_PATH", "RTK_TEE_DIR", "RTK_RECALL_DB", "RTK_CONFIG_DIR"):
            self.assertTrue(env[key].startswith(env["COPILOT_S_RUN_DIR"] + "/raw/rtk/"))
        self.assertEqual(self.records()[0]["mode"], "rtk")
        # A fresh wrapper process does not remember --rtk.
        self.launch("--name", "Fixture", full_main=False, resume="wrapper-session")
        self.assertEqual(self.captured()["args"][0], "--resume=wrapper-session")
        self.assertNotIn("--plugin-dir", self.captured()["args"])
        self.assertEqual({r["mode"] for r in self.records()}, {"normal", "rtk"})

    def test_operands_and_double_dash_do_not_activate_rtk_or_wrapper_help(self):
        for args in (["--prompt", "--rtk"], ["--prompt", "--help"], ["--prompt", "--version"],
                     ["--", "--rtk", "--help", "--version", "--session-report", "not-a-session"]):
            with self.subTest(args=args):
                self.launch("--name", "Fixture", *args)
                argv = self.captured()["args"]
                self.assertNotIn("--plugin-dir", argv)
                self.assertEqual(argv[2:2 + len(args)] if args[0] != "--" else argv[-len(args):], args)
                self.assertTrue(all(r["mode"] == "normal" for r in self.records()))

    def test_missing_rtk_or_unsupported_version_never_launches_copilot(self):
        for version in ("9.0.0", "missing"):
            with self.subTest(version=version):
                if version == "missing":
                    self.rtk.unlink()
                else:
                    self.env["TEST_RTK_VERSION"] = version
                self.launch("--rtk", expected=1)
                self.assertFalse((self.home / "captured.json").exists())
        self.assertEqual(self.records(), [])

    def test_helper_degradation_in_normal_mode_and_fail_closed_rtk_start(self):
        helper = fixture.write(self.home / "boundary-helper.py", r'''
import os, pathlib, sys
with open(os.environ["TEST_HELPER_LOG"], "a") as log:
    log.write(sys.argv[1] + "\n")
command = sys.argv[1]
mode = os.environ["TEST_HELPER_MODE"]
if command == "preflight":
    sys.exit(0)
if command == "start" and mode == "start-fail":
    print("synthetic start failure", file=sys.stderr)
    sys.exit(7)
if command == "start" and mode == "empty-run-dir":
    print("USAGE_FILE\t" + os.path.join(os.environ["HOME"], "unused-usage.json"))
    sys.exit(0)
if command == "start" and mode == "incomplete-env":
    base = os.path.join(os.environ["HOME"], "synthetic-run")
    print("RUN_DIR\t" + base)
    print("RTK_BIN\t" + os.path.join(os.environ["HOME"], "fake-bin", "rtk"))
    for key in ("RTK_DB_PATH", "RTK_TEE_DIR", "RTK_RECALL_DB", "RTK_CONFIG_DIR"):
        print(key + "\t" + base + "/" + key)
    sys.exit(0)  # Deliberately omit the fifth required isolation key.
if command == "finish" and mode == "finish-fail":
    print("synthetic finish failure", file=sys.stderr)
    sys.exit(9)
os.execv(sys.executable, [sys.executable, os.environ["TEST_REAL_REPORT_HELPER"], *sys.argv[1:]])
''')
        log = self.home / "boundary-helper-calls"
        common = {"TEST_HELPER_LOG": str(log), "TEST_REAL_REPORT_HELPER": str(fixture.HELPER)}
        for mode, warning in (("missing", "helper not found"), ("start-fail", "not started"),
                              ("empty-run-dir", "no run folder"), ("finish-fail", "finalization had problems")):
            with self.subTest(normal=mode), mock.patch.dict(self.env, dict(common,
                    COPILOT_SESSION_REPORT_HELPER=str(self.home / "missing-helper") if mode == "missing" else str(helper),
                    TEST_HELPER_MODE=mode)):
                log.unlink(missing_ok=True)
                (self.home / "captured.json").unlink(missing_ok=True)
                result = self.launch("--name", "Degraded fixture", "--model=discarded")
                self.assertIn("Warning", result.stderr)
                self.assertIn(warning, result.stderr)
                self.assertEqual(self.captured()["args"][:6],
                                 ["--name", "Degraded fixture", "--model", "auto", "--auto-tier", "balance"])
                if mode == "finish-fail":
                    self.assertEqual(log.read_text().splitlines(), ["start", "finish"])
        for mode in ("missing", "start-fail", "empty-run-dir", "incomplete-env"):
            with self.subTest(rtk=mode), mock.patch.dict(self.env, dict(common,
                    COPILOT_SESSION_REPORT_HELPER=str(self.home / "missing-helper") if mode == "missing" else str(helper),
                    TEST_HELPER_MODE=mode)):
                (self.home / "captured.json").unlink(missing_ok=True)
                log.unlink(missing_ok=True)
                result = self.launch("--rtk", "--name", "Never launched", expected=1)
                self.assertFalse((self.home / "captured.json").exists())
                self.assertIn("Nothing was launched", result.stderr)
                if mode != "missing":
                    self.assertEqual(log.read_text().splitlines(), ["preflight", "start"])

    def test_additional_directories_are_checked_without_changing_native_operands(self):
        clean = self.cwd / "clean-additional"
        clean.mkdir()
        other = self.home / "extra-repository"
        other.mkdir()
        (other / ".git").mkdir()
        hook = fixture.write(other / ".github/copilot/settings.json",
                             '{"hooks":{"PreToolUse":[{"hooks":[{"command":"rtk hook copilot"}]}]}}')
        relative = self.cwd / "relative-additional"
        relative.symlink_to(other, target_is_directory=True)
        contaminated = [
            ["--add-dir", str(other)], ["--add-dir=" + str(other)],
            ["--add-dir", str(clean), "--add-dir=" + str(other)],
            ["--add-dir", "relative-additional"],
        ]
        for args in contaminated:
            with self.subTest(contaminated=args):
                (self.home / "captured.json").unlink(missing_ok=True)
                self.launch("--rtk", "--name", "Fixture", *args, expected=1)
                self.assertFalse((self.home / "captured.json").exists())
        previous = {r["run_id"] for r in self.records()}
        self.launch("--name", "Fixture", "--add-dir", str(other))
        self.assertTrue(next(r for r in self.records() if r["run_id"] not in previous)["baseline_notes"])
        hook.unlink()
        c_target = self.home / "different-cwd"
        target_add = c_target / "target-additional"
        target_add.mkdir(parents=True)
        target_hook = fixture.write(target_add / ".github/copilot/settings.local.json",
                                    '{"command":"rtk hook copilot"}')
        args = ["-C", str(c_target), "--add-dir=target-additional"]
        self.launch("--rtk", "--name", "Fixture", *args, expected=1)
        target_hook.unlink()
        fixture.write(self.states / "session-a/workspace.yaml", "cwd: " + str(self.cwd) + "\n")
        for args in (["--add-dir", str(clean), "--add-dir=" + str(other)],
                     ["--add-dir=relative-additional"],
                     ["-C", str(c_target), "--add-dir=target-additional"],
                     ["--resume=session-a", "--add-dir", str(clean)],
                     ["--session-id=new-additional-session", "--add-dir=" + str(clean)]):
            with self.subTest(clean=args):
                self.launch("--rtk", "--name", "Fixture", *args)
                self.assertEqual(self.captured()["args"][:2 + len(args)], ["--name", "Fixture"] + args)
        uncertain = [
            ["--add-dir="], ["--add-dir", ""], ["--add-dir", "--"],
            ["--add-dir=" + str(self.home / "missing-additional")],
            ["--resume=session-a", "--add-dir", "clean-additional"],
            ["--session-id=new-additional-session", "--add-dir=clean-additional"],
        ]
        for args in uncertain:
            with self.subTest(uncertain=args):
                (self.home / "captured.json").unlink(missing_ok=True)
                self.launch("--rtk", "--name", "Fixture", *args, expected=1)
                self.assertFalse((self.home / "captured.json").exists())
                previous = {r["run_id"] for r in self.records()}
                self.launch("--name", "Fixture", *args)
                self.assertTrue(next(r for r in self.records() if r["run_id"] not in previous)["baseline_notes"])
        # Neither an option-looking prompt nor positional tokens after -- are
        # additional-directory options, even if they name a missing directory.
        missing = str(self.home / "not-an-additional-directory")
        for tail in (["--prompt", "--add-dir=" + missing], ["--prompt", "--add-dir"],
                     ["--", "--add-dir", missing]):
            with self.subTest(boundary=tail):
                self.launch("--rtk", "--name", "Fixture", *tail)
                args = self.captured()["args"]
                self.assertEqual(args[-len(tail):] if tail[0] == "--" else args[2:2 + len(tail)], tail)

    def test_incomplete_actual_rtk_mode_never_silently_falls_back(self):
        self.env["TEST_RTK_REPLY"] = "malformed"
        self.launch("--rtk", expected=1)
        self.assertFalse((self.home / "captured.json").exists())
        self.assertEqual(self.records()[0]["rtk"]["preflight"]["smoke_test"], "failed")

    def test_caller_extra_plugin_cannot_double_filter_rtk(self):
        extra = self.home / "caller-plugin"
        fixture.write(extra / ".claude-plugin/plugin.json", '{"name":"external-rtk","version":"1.0.0"}')
        fixture.write(extra / "hooks/hooks.json", json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook copilot"}]}]}}))
        for plugin_args in (["--plugin-dir", str(extra)], ["--plugin-dir=" + str(extra)]):
            with self.subTest(external=plugin_args):
                result = self.launch("--rtk", *plugin_args, "--name", "Fixture", expected=1)
                self.assertIn("rtk hook", result.stderr)
                self.assertFalse((self.home / "captured.json").exists(),
                                 "caller plugin must not bypass double-filter preflight")

        safe = self.cwd / "safe-plugin"
        fixture.write(safe / ".claude-plugin/plugin.json", '{"name":"safe-fixture","version":"1.0.0"}')
        fixture.write(safe / "hooks/hooks.json", '{"hooks":{}}')
        plugin_bytes = {str(p): p.read_bytes() for p in safe.rglob("*") if p.is_file()}
        fixture.write(self.states / "session-a/workspace.yaml", "cwd: " + str(self.cwd) + "\n")
        # A new launch has a definite cwd. Known -C/resume selectors still
        # require absolute plugin paths. Unknown selectors are tested below.
        for context in ([], ["-C", str(self.cwd)], ["-C" + str(self.cwd)],
                        ["--resume=session-a"], ["--session-id", "session-a"]):
            for absolute in (False, True):
                plugin_path = str(safe) if absolute else "./safe-plugin"
                for equals in (False, True):
                    plugin_args = ["--plugin-dir=" + plugin_path] if equals else ["--plugin-dir", plugin_path]
                    refuse = bool(context) and not absolute
                    with self.subTest(context=context, absolute=absolute, equals=equals):
                        (self.home / "captured.json").unlink(missing_ok=True)
                        result = self.launch("--rtk", "--name", "Fixture", *context, *plugin_args,
                                             expected=1 if refuse else 0)
                        if refuse:
                            self.assertIn("absolute path", result.stderr)
                            self.assertFalse((self.home / "captured.json").exists())
                        else:
                            args = self.captured()["args"]
                            self.assertEqual(args[-2:], ["--plugin-dir", str(fixture.PLUGIN)])
                            for arg in context + plugin_args:
                                self.assertIn(arg, args)
                            self.assertFalse(any(a.startswith(("--allow", "--yolo")) for a in args))
        self.assertEqual({str(p): p.read_bytes() for p in safe.rglob("*") if p.is_file()}, plugin_bytes)

        # Plugin-looking prompt operands and tokens after -- are not options.
        for tail in (["--prompt", "--plugin-dir=" + str(extra)],
                     ["--", "--plugin-dir", str(extra)]):
            with self.subTest(boundary=tail):
                self.launch("--rtk", "--name", "Fixture", *tail)
                args = self.captured()["args"]
                if tail[0] == "--":
                    self.assertEqual(args[-len(tail):], tail)
                else:
                    self.assertEqual(args[2:2 + len(tail)], tail)
        self.launch("--rtk", "--name", "Fixture", "--prompt", "--worktree",
                    "--plugin-dir", "./safe-plugin")

        # Inspect refusal behavior through the real helper command without
        # launching Copilot or writing any candidate plugin.
        link = self.home / "linked-plugin"
        link.symlink_to(safe, target_is_directory=True)
        regular = fixture.write(self.home / "not-a-directory", "plain file")
        large = self.home / "large-plugin"
        fixture.write(large / "asset.txt", "x" * (1024 * 1024 + 1))
        deep = self.home / "deep-plugin"
        fixture.write(deep.joinpath(*(["nested"] * 9), "asset.txt"), "plain file")
        linked_file = self.home / "plugin-with-link"
        linked_file.mkdir()
        (linked_file / "asset.txt").symlink_to(regular)
        for path in (self.home / "missing-plugin", link, regular, fixture.PLUGIN,
                     large, deep, linked_file):
            with self.subTest(unverifiable=str(path.name)):
                self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                         "--plugin-dir", fixture.PLUGIN, "--extra-plugin-dir=" + str(path), expected=3)
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                 "--plugin-dir", fixture.PLUGIN, "--extra-plugin-dir=", expected=3)
        # Exercise the entry cap without making 20,001 files.
        with mock.patch.object(self.mod.os, "walk",
                               return_value=iter([(str(safe), [], ["f%s" % i for i in range(20001)])])):
            problems = self.mod.caller_plugin_problems([str(safe)], str(self.cwd), False, str(fixture.PLUGIN))
        self.assertTrue(any("20000" in reason for _, reason in problems), problems)

    def test_actual_project_dirs_check_other_repo_git_root_and_clean_launches(self):
        other = self.home / "other-repo"
        nested = other / "src"
        nested.mkdir(parents=True)
        (other / ".git").mkdir()
        hook = fixture.write(other / ".github/hooks/filter.json", json.dumps({
            "version": 1, "hooks": {"preToolUse": [{"type": "command", "bash": "rtk hook copilot"}]}}))
        fixture.write(self.states / "foreign-session/workspace.yaml", "cwd: " + str(nested) + "\n")
        cases = [
            ["-C", str(nested)], ["-C" + str(nested)],
            ["-C", os.path.relpath(nested, self.cwd)],
            ["--resume=foreign-session"], ["-rforeign-session"],
            ["--session-id", "foreign-session"], ["--session-id=foreign-session"],
        ]
        for args in cases:
            with self.subTest(contaminated=args):
                (self.home / "captured.json").unlink(missing_ok=True)
                self.launch("--rtk", "--name", "Fixture", *args, expected=1)
                self.assertFalse((self.home / "captured.json").exists())
        # Normal mode remains runnable but must disclose contamination.
        previous = {r["run_id"] for r in self.records()}
        self.launch("--name", "Fixture", "-C", str(nested))
        new = [r for r in self.records() if r["run_id"] not in previous]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0]["baseline_notes"])
        self.assertEqual(self.captured()["args"][:4], ["--name", "Fixture", "-C", str(nested)])
        hook.unlink()
        for args in (["-C", str(nested)], ["--session-id", "foreign-session"],
                     ["--session-id", "brand-new-session-id"]):
            with self.subTest(clean=args):
                self.launch("--rtk", "--name", "Fixture", *args)
                for arg in args:
                    self.assertIn(arg, self.captured()["args"])
        # Apparent selectors used as operands/positionals do not change cwd.
        hook.write_text('{"command":"rtk hook copilot"}')
        for tail in (["--prompt", "-C" + str(nested)],
                     ["--prompt", "--resume=foreign-session"],
                     ["--", "-C", str(nested), "--resume=foreign-session"]):
            with self.subTest(boundary=tail):
                self.launch("--rtk", "--name", "Fixture", *tail)
        missing = self.home / "missing-project"
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                 "--project-dir=" + str(missing), expected=3)

    def test_unknown_or_unreadable_session_cwd_refuses_rtk_warns_normal(self):
        fixture.write(self.states / "relative-session/workspace.yaml", "cwd: ../relative\n")
        fixture.write(self.states / "quoted-session/workspace.yaml", 'cwd: "' + str(self.cwd) + '"\n')
        fixture.write(self.states / "missing-project-session/workspace.yaml",
                      "cwd: " + str(self.home / "missing-project") + "\n")
        unknown = [
            ["--resume"], ["-r"], ["--continue"], ["--connect=fixture"],
            ["--worktree"], ["--worktree=fixture"], ["-C"], ["-C", "--"],
            ["--resume=unknown-session"], ["--resume=relative-session"],
            ["--resume=quoted-session"], ["--resume=missing-project-session"],
            ["--resume=../escape"],
        ]
        for args in unknown:
            with self.subTest(unknown=args):
                (self.home / "captured.json").unlink(missing_ok=True)
                self.launch("--rtk", "--name", "Fixture", *args, expected=1)
                self.assertFalse((self.home / "captured.json").exists())
                previous = {r["run_id"] for r in self.records()}
                self.launch("--name", "Fixture", *args)
                new = [r for r in self.records() if r["run_id"] not in previous]
                self.assertEqual(len(new), 1)
                self.assertTrue(new[0]["baseline_notes"], "normal mode must disclose an unverified baseline")
                captured = self.captured()["args"]
                for arg in args:
                    self.assertIn(arg, captured)
        # An existing selected session is not equivalent to a new session id:
        # with no readable absolute cwd its destination cannot be certified.
        (self.states / "selected-missing-cwd").mkdir()
        (self.states / "selected-dangling").symlink_to(self.home / "missing-selected-target")
        fixture.write(self.states / "selected-file", "not a session directory")
        for args in (["--session-id", "selected-missing-cwd"],
                     ["--session-id=relative-session"], ["--session-id=selected-dangling"],
                     ["--session-id", "selected-file"], ["--session-id=../escape"]):
            with self.subTest(selected_unreadable=args):
                (self.home / "captured.json").unlink(missing_ok=True)
                self.launch("--rtk", "--name", "Fixture", *args, expected=1)
                self.assertFalse((self.home / "captured.json").exists())
                previous = {r["run_id"] for r in self.records()}
                self.launch("--name", "Fixture", *args)
                new = [r for r in self.records() if r["run_id"] not in previous]
                self.assertEqual(len(new), 1)
                self.assertTrue(new[0]["baseline_notes"])
                for arg in args:
                    self.assertIn(arg, self.captured()["args"])

    def test_real_bash_help_version_and_report_are_side_effect_free(self):
        for args in (["--help"], ["--version"], ["--rtk", "--help"],
                     ["--session-report", "historical-untracked"]):
            with self.subTest(args=args):
                before = self.snapshot()
                proc = subprocess.run(["/bin/bash", str(fixture.LAUNCHER), *args],
                                      env=self.env, cwd=self.cwd, text=True,
                                      capture_output=True, timeout=10)
                self.assertEqual(proc.returncode, 1 if args[0] == "--session-report" else 0,
                                 proc.stdout + proc.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.home / ".copilot/otel").exists())
                self.assertFalse((self.home / "captured.json").exists())


class TestSessionReportDistribution(fixture.ReportHome):
    def test_copy_and_symlink_dependencies_assets_uninstall_preserves_unmanaged(self):
        run_id = self.start(otel=self.otel(fixture.span()))
        self.prices()
        self.finish(run_id)
        preserved = self.snapshot()
        for copied in (False, True):
            with self.subTest(copy=copied):
                prefix = self.home / ("copied bin" if copied else "linked bin")
                args = ["/bin/bash", str(fixture.ROOT / "scripts/install.sh"), "--prefix", str(prefix)]
                if copied:
                    args += ["--copy"]
                result = subprocess.run(args, env=self.env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                relative = ["copilot-session-report.py", "copilot-s-rtk-plugin/.claude-plugin/plugin.json",
                            "copilot-s-rtk-plugin/hooks/hooks.json", "copilot-s-rtk-plugin/hooks/copilot-s-rtk-hook.py"]
                manifest = (self.home / ".copilot-cli-toolkit/install-manifest.txt").read_text().splitlines()
                for name in relative:
                    installed = prefix / name
                    self.assertEqual(installed.is_symlink(), not copied)
                    self.assertEqual(installed.read_bytes(), (fixture.ROOT / "bin" / name).read_bytes())
                    self.assertIn(str(installed), manifest)
                self.assertTrue(os.access(prefix / relative[0], os.X_OK))
                trace = subprocess.run(["/bin/bash", "-x", str(prefix / "copilot-s"), "--version"],
                                       env=self.env, text=True, capture_output=True, timeout=10)
                self.assertEqual(trace.returncode, 0, trace.stderr)
                expected_helper = prefix / relative[0] if copied else fixture.HELPER
                # This test checks installed resolution, not the test override.
                clean_env = {k: v for k, v in self.env.items()
                             if k not in ("COPILOT_SESSION_REPORT_HELPER", "COPILOT_S_RTK_PLUGIN_DIR")}
                trace = subprocess.run(["/bin/bash", "-x", str(prefix / "copilot-s"), "--version"],
                                       env=clean_env, text=True, capture_output=True, timeout=10)
                resolved = next(line.split("=", 1)[1].strip("'") for line in trace.stderr.splitlines()
                                if line.startswith("+ SESSION_REPORT_HELPER="))
                self.assertEqual(Path(resolved).resolve(), expected_helper.resolve())
                usage = subprocess.run([sys.executable, str(prefix / relative[0]), "launch", "--run-id",
                                        run_id, "--format", "json"], env=self.env,
                                       text=True, capture_output=True, timeout=10)
                self.assertEqual(usage.returncode, 0, usage.stderr)
                # Finish a new run through the installed helper to execute its
                # sibling pricing-module dependency, not just --help.
                pending = self.start(otel=self.otel(fixture.span()))
                price = subprocess.run([sys.executable, str(prefix / relative[0]), "finish", "--run-id",
                    pending, "--exit-code", "0", "--session-id", "session-a"], env=self.env,
                    text=True, capture_output=True, timeout=10)
                self.assertEqual(price.returncode, 0, price.stdout + price.stderr)
                self.assertEqual(self.record(pending)["cost"]["availability"], "complete")
                unmanaged = fixture.write(prefix / "copilot-s-rtk-plugin/hooks/my-notes.txt", "keep")
                removed = subprocess.run(["/bin/bash", str(fixture.ROOT / "scripts/uninstall.sh")],
                                         env=self.env, text=True, capture_output=True, timeout=30)
                self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
                for name in relative:
                    self.assertFalse(os.path.lexists(prefix / name))
                self.assertEqual(unmanaged.read_text(), "keep")
                after = self.snapshot()
                for name, value in preserved.items():
                    self.assertEqual(after[name], value, name)


if __name__ == "__main__":
    unittest.main()
