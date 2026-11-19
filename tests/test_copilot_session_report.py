#!/usr/bin/env python3
"""Hermetic per-launch reporting tests. No hosted calls or real user state.

The fake RTK implements the documented CLI boundary, not the reporter's
internals. Prices are synthetic official-schema fixtures, never live rates.
"""
import concurrent.futures
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import test_copilot_task_report as pricing_fixture

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin/copilot-session-report.py"
PLUGIN = ROOT / "bin/copilot-s-rtk-plugin"
HOOK = PLUGIN / "hooks/copilot-s-rtk-hook.py"
LAUNCHER = ROOT / "bin/copilot-s"

FAKE_RTK = r"""
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("rtk " + os.environ.get("TEST_RTK_VERSION", "1.2.0"))
elif args == ["hook", "copilot"]:
    command = json.load(sys.stdin)["tool_input"]["command"]
    mode = os.environ.get("TEST_RTK_REPLY", "rewrite")
    counter = os.environ.get("TEST_RTK_HOOK_COUNT")
    if counter:
        path = pathlib.Path(counter)
        path.write_text(str(int(path.read_text()) + 1 if path.exists() else 1))
    if mode == "error":
        sys.exit(9)
    if mode == "malformed":
        print("{")
    elif mode == "array":
        print("[]")
    elif mode in ("decision", "decision-deny"):
        print(json.dumps({"hookSpecificOutput": {"permissionDecision":
            "deny" if mode == "decision-deny" else "allow",
            "updatedInput": {"command": "rtk " + command}}}))
    elif mode == "empty":
        pass
    elif mode == "nonexact":
        print(json.dumps({"hookSpecificOutput": {"updatedInput": {"command": "rtk git diff"}}}))
    elif mode == "bad-command":
        print(json.dumps({"hookSpecificOutput": {"updatedInput": {"command": False}}}))
    elif mode == "rewrite-all" or command.startswith(("cargo ", "pytest", "npm run ", "go ", "make ")):
        print(json.dumps({"hookSpecificOutput": {
            "updatedInput": {"command": "rtk " + command}}}))
elif args == ["gain", "--format", "json"]:
    db = pathlib.Path(os.environ["RTK_DB_PATH"])
    # A test DB is a fixture consumed only by this fake binary.
    print(db.read_text())
else:
    sys.exit(8)
"""

GAIN = {"summary": {"total_commands": 2, "total_input": 1000, "total_output": 200,
                   "total_saved": 800, "total_time_ms": 400, "avg_time_ms": 200,
                   "oversized_excluded": 0, "avg_savings_pct": 80,
                   "raw_command": "PRIVATE_COMMAND_SENTINEL"},
        "commands": [{"command": "PRIVATE_COMMAND_SENTINEL"}]}


def load_report():
    spec = importlib.util.spec_from_file_location("session_report_under_test", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(path, text, executable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o700)
    return path


def span(sid="session-a", model="gpt-6-luna", number=1, **attrs):
    attributes = {"gen_ai.conversation.id": sid, "gen_ai.response.model": model,
                  "gen_ai.usage.input_tokens": 1000, "gen_ai.usage.output_tokens": 100,
                  "gen_ai.usage.cache_read.input_tokens": 200,
                  "gen_ai.usage.cache_write.input_tokens": 100,
                  "gen_ai.usage.reasoning.output_tokens": 20,
                  "prompt": "PRIVATE_PROMPT_SENTINEL"}
    attributes.update(attrs)
    return {"type": "span", "name": "chat " + model, "traceId": "trace-a",
            "spanId": "span-%s" % number, "startTime": [100, 0],
            "endTime": [101, 500000000], "attributes": attributes}


class ReportHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="session-report-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.cwd = self.home / "project"
        self.cwd.mkdir()
        self.bin = self.home / "fake-bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.rtk = write(self.bin / "rtk", "#!%s\n%s" % (sys.executable, FAKE_RTK), True)
        # A minimal, scrubbed environment: no credentials, provider endpoints,
        # inherited Copilot/RTK config, extensions or real user directories.
        self.env = {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin,
                    "LANG": "en_US.UTF-8", "TERM": "dumb",
                    "TMPDIR": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "COPILOT_HOME": str(self.home / ".copilot"),
                    "XDG_CONFIG_HOME": str(self.home / "xdg-config"),
                    "XDG_DATA_HOME": str(self.home / "xdg-data"),
                    "COPILOT_TASK_REPORT_PRICING_FETCH": "0"}
        # start/finish now also reconcile/publish in-process. Isolate those
        # paths just as strictly as subprocesses, not only the CLI helper.
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.mod = load_report()
        self.states = self.home / ".copilot/session-state"
        (self.states / "session-a").mkdir(parents=True)

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(HELPER), *map(str, args)],
                                cwd=self.cwd, env=env or self.env, text=True,
                                capture_output=True, timeout=25)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def start(self, mode="normal", sid=None, owner="owned", otel=None, extra=()):
        run_id = str(uuid.uuid4())
        args = ["start", "--run-id", run_id, "--mode", mode, "--cwd", self.cwd,
                "--launch-kind", "resume" if sid else "new", "--usage-file-owner", owner,
                "--plugin-dir", PLUGIN, "--copilot-s-version", "0.0.0-test"]
        if sid:
            args += ["--resume-session-id", sid]
        if otel is not None:
            args += ["--otel-file", otel]
        args += list(extra)
        self.cli(*args)
        return run_id

    def finish(self, run_id, sid="session-a", code=0, expected=0):
        args = ["finish", "--run-id", run_id, "--exit-code", code,
                "--launched-epoch", 100, "--ended-epoch", 103,
                "--session-state-dir", self.states]
        if sid:
            args += ["--session-id", sid]
        self.cli(*args, expected=expected)
        return self.record(run_id)

    def path(self, run_id):
        return self.home / ".copilot/session-reports/runs" / run_id

    def record(self, run_id):
        return json.loads((self.path(run_id) / "run.json").read_text())

    def otel(self, *events):
        path = self.home / ("copilot-otel-" + uuid.uuid4().hex + ".jsonl")
        write(path, "".join(json.dumps(e) + "\n" for e in events))
        return path

    def prices(self, age=0, markdown=None):
        tr = pricing_fixture.load_module()
        snapshot = tr.build_official_snapshot(
            (markdown or pricing_fixture.OFFICIAL_PRICING_MARKDOWN).encode(),
            time.time() - age)
        write(self.home / ".copilot/task-reports/official-pricing-cache.json",
              json.dumps(snapshot))
        return snapshot

    def gain(self, run_id, data=None):
        write(self.path(run_id) / "raw/rtk/history.db", json.dumps(data or GAIN))

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns,
                                                stat.S_IMODE(p.stat().st_mode))
                for p in self.home.rglob("*") if p.is_file() and not p.is_symlink()}


class TestLifecycleAndAttribution(ReportHome):
    def test_exact_launch_resume_dedup_and_frozen_cache_price(self):
        snapshot = self.prices()
        first = span()
        other_trace = dict(first, traceId="trace-b")
        unkeyed = dict(first)
        unkeyed.pop("traceId")
        no_usage = span(number=8)
        no_usage["attributes"] = {"gen_ai.conversation.id": "session-a"}
        path = self.otel(first, first, other_trace, unkeyed, no_usage,
                         span("foreign-session", number=9))
        # A different launch's huge span is intentionally never scanned.
        write(self.home / ".copilot/otel/copilot-otel-other.jsonl",
              json.dumps(span(number=99, **{"gen_ai.usage.input_tokens": 999999})))
        run_id = self.start(sid="session-a", otel=path)
        rec = self.finish(run_id, sid="ignored-launcher-id")
        self.assertEqual(rec["session"]["id"], "session-a")
        self.assertEqual(rec["usage"]["calls"], 2)
        self.assertEqual(rec["usage"]["input_tokens"], 2000)
        self.assertEqual(rec["usage"]["cache_read_tokens"], 400)
        self.assertEqual(rec["usage"]["cache_write_tokens"], 200)
        self.assertEqual(rec["usage"]["reasoning_tokens"], 40)
        self.assertEqual(rec["usage"]["duplicate_spans_ignored"], 1)
        self.assertEqual(rec["usage"]["chat_spans_without_stable_ids"], 1)
        self.assertEqual(rec["usage"]["chat_spans_without_usage"], 1)
        self.assertEqual(rec["otel"]["chat_spans_other_conversations"], 1)
        self.assertEqual(rec["cost"]["priced_calls"], 2)
        self.assertEqual(rec["cost"]["snapshot"]["id"], snapshot["snapshot_id"])
        # 700 fresh + 200 cached + 100 writes; reasoning is not added twice.
        self.assertAlmostEqual(rec["cost"]["estimated_usd"], 2 * (
            700 * .1 + 200 * .01 + 100 * .125 + 100 * .5) / 1e6)
        before = (self.path(run_id) / "run.json").read_bytes()
        changed = self.prices(markdown=pricing_fixture.OFFICIAL_PRICING_MARKDOWN.replace(
            "| $0.1 | $0.01 |", "| $0.2 | $0.01 |"))
        self.assertNotEqual(changed["snapshot_id"], snapshot["snapshot_id"])
        self.cli("finish", "--run-id", run_id, "--exit-code", 7)
        self.assertEqual((self.path(run_id) / "run.json").read_bytes(), before)
        exported = self.cli("launch", "--run-id", run_id, "--format", "json")
        self.assertEqual(json.loads(exported.stdout), rec)
        self.assertNotIn("PRIVATE_PROMPT_SENTINEL", exported.stdout)
        self.assertIn("<$0.01", self.cli("launch", "--run-id", run_id).stdout)

    def test_conflicting_new_resume_and_ambiguous_evidence_is_not_latest(self):
        for resumed, launcher, conversations, hooks, expected_sid in [
            (None, "session-a", ["foreign"], [], None),
            ("session-a", None, ["foreign"], [], None),
            (None, "session-a", ["session-a"], ["foreign"], None),
            (None, None, ["session-a", "foreign"], [], None),
            (None, None, ["session-a"], ["session-a"], "session-a"),
            (None, "session-a", ["session-a", "foreign"], ["session-a"], "session-a"),
        ]:
            with self.subTest(resumed=resumed, launcher=launcher, conversations=conversations, hooks=hooks):
                run_id = self.start("rtk", sid=resumed,
                                    otel=self.otel(*(span(s) for s in conversations)))
                write(self.path(run_id) / "hook-events.jsonl", "".join(
                    json.dumps({"sid": s, "decision": "unchanged", "reason": "not-allowlisted"}) + "\n"
                    for s in hooks))
                rec = self.finish(run_id, sid=launcher)
                self.assertEqual(rec["session"]["id"], expected_sid)
                if expected_sid is None:
                    self.assertIsNone(rec["cost"]["estimated_usd"])
                    self.assertEqual(rec["usage"]["availability"], "unavailable")

    def test_unknown_zero_and_missing_telemetry_are_distinct(self):
        for events, configure, expected_usage, expected_cost in [
            ([span(model="unlisted-model")], True, "complete", None),
            ([span()], True, "complete", None),  # no cache is not free
            ([], True, "none-observed", 0.0),
            ([], False, "unavailable", None),
        ]:
            with self.subTest(events=events, configure=configure):
                run_id = self.start(otel=self.otel(*events) if configure else None)
                rec = self.finish(run_id)
                self.assertEqual(rec["usage"]["availability"], expected_usage)
                self.assertEqual(rec["cost"]["estimated_usd"], expected_cost)

    def test_invalid_finite_integer_usage_never_prices_or_silently_truncates(self):
        self.prices()
        for bad in (True, -1, 1.5, "12", 2**53 + 1, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                run_id = self.start(otel=self.otel(span(**{"gen_ai.usage.input_tokens": bad})))
                rec = self.finish(run_id)
                self.assertEqual(rec["usage"]["invalid_usage_calls"], 1)
                self.assertEqual(rec["usage"]["availability"], "partial")
                self.assertIsNone(rec["cost"]["estimated_usd"])
                self.assertEqual(rec["cost"]["unpriced_calls"], 1)

    def test_optional_token_presence_survives_into_report_not_silent_zero(self):
        self.prices()
        event = span()
        for attr in ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_write.input_tokens",
                     "gen_ai.usage.reasoning.output_tokens"):
            del event["attributes"][attr]
        absent = self.finish(self.start(otel=self.otel(event)))["usage"]
        zero = self.finish(self.start(otel=self.otel(span(**{
            "gen_ai.usage.cache_read.input_tokens": 0, "gen_ai.usage.cache_write.input_tokens": 0,
            "gen_ai.usage.reasoning.output_tokens": 0}))))["usage"]
        self.assertNotEqual(absent, zero,
                            "Unavailable optional token fields must be distinguishable from measured zero")

    def test_expired_cache_unpriced_and_snapshot_never_fetched(self):
        self.prices(age=8 * 86400)
        rec = self.finish(self.start(otel=self.otel(span())))
        self.assertEqual(rec["cost"]["availability"], "unavailable")
        self.assertIsNone(rec["cost"]["estimated_usd"])
        self.assertEqual(rec["cost"]["unpriced_calls"], 1)
        with mock.patch.dict(os.environ, self.env, clear=True):
            tr = self.mod.load_task_report_module()
            with mock.patch.object(tr, "fetch_official_pricing_bytes",
                                   side_effect=AssertionError("network forbidden"), create=True):
                with mock.patch.object(self.mod, "load_task_report_module", return_value=tr):
                    self.mod.usage_and_cost([span()])

    def test_atomic_private_records_and_concurrent_finalize_outcome(self):
        run_id = self.start(otel=self.otel(span()))
        rdir = self.path(run_id)
        for d in (rdir, rdir.parent, rdir.parent.parent, rdir / "raw"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((rdir / "run.json").stat().st_mode), 0o600)
        commands = [
            ["finish", "--run-id", run_id, "--exit-code", "130", "--session-id", "session-a"],
            ["mark-outcome", "--run-id", run_id, "--outcome", "completed"],
        ] * 4
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda a: self.cli(*a), commands))
        rec = self.record(run_id)
        self.assertEqual(rec["state"], "interrupted")
        self.assertEqual(rec["task_outcome"]["value"], "completed")
        self.assertEqual(rec["task_outcome"]["source"], "user-marked")
        self.assertEqual(list(rdir.glob(".tmp-*")), [])
        self.assertEqual(stat.S_IMODE((rdir / ".lock").stat().st_mode), 0o600)
        self.cli("mark-outcome", "--run-id", run_id, "--outcome", "unknown")
        self.assertEqual(self.record(run_id)["task_outcome"]["source"], "user-reset")
        self.cli("start", "--run-id", run_id, "--mode", "normal", "--cwd", self.cwd,
                 "--launch-kind", "new", "--usage-file-owner", "owned", expected=2)

    def test_mixed_session_compare_outcomes_and_read_only_exports(self):
        normal = self.start(otel=self.otel(span()))
        filtered = self.start("rtk", sid="session-a", otel=self.otel(span()))
        failed = self.start(sid="session-a")
        self.finish(normal)
        self.finish(filtered, code=130)
        self.finish(failed, code=7)
        self.cli("mark-outcome", "--run-id", filtered, "--outcome", "abandoned")
        before = self.snapshot()
        session = json.loads(self.cli("session", "--session-id", "session-a", "--format", "json").stdout)
        self.assertTrue(session["mixed_modes"])
        self.assertEqual(session["modes_present"], ["normal", "rtk"])
        self.assertEqual(session["by_mode"]["normal"]["states"],
                         {"completed": 1, "copilot-error": 1})
        self.assertEqual(session["by_mode"]["rtk"]["task_outcomes"], {"abandoned": 1})
        self.assertIn("observational", " ".join(session["notes"]))
        comparison = json.loads(self.cli("compare", "--format", "json").stdout)
        self.assertEqual(comparison["by_mode"], session["by_mode"])
        self.cli("compare")
        self.cli("session", "--session-id", "historical-untracked", expected=1)
        self.cli("launch", "--run-id", normal)
        self.assertEqual(self.snapshot(), before, "exports must not create DBs, reprice or rewrite records")

    def test_invalid_run_records_are_skipped_and_launch_errors_are_clear(self):
        valid = self.start(otel=self.otel(span()))
        self.finish(valid)
        template = self.record(valid)
        failures = [
            ("corrupt-json", "{", 2),
            ("wrong-schema", dict(template, schema="unsupported"), 2),
            ("wrong-version", dict(template, schema_version=999), 2),
            ("wrong-id", dict(template, run_id=str(uuid.uuid4())), 2),
            ("symlink", None, 1),
        ]
        for label, payload, code in failures:
            with self.subTest(record=label):
                run_id = str(uuid.uuid4())
                path = self.path(run_id) / "run.json"
                if label == "symlink":
                    path.parent.mkdir()
                    path.symlink_to(self.path(valid) / "run.json")
                else:
                    if isinstance(payload, dict) and label != "wrong-id":
                        payload = dict(payload, run_id=run_id)
                    write(path, json.dumps(payload) if isinstance(payload, dict) else payload)
                result = self.cli("launch", "--run-id", run_id, "--format", "json", expected=code)
                self.assertIn("copilot-session-report:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
        before = self.snapshot()
        comparison = json.loads(self.cli("compare", "--format", "json").stdout)
        session = json.loads(self.cli("session", "--session-id", "session-a", "--format", "json").stdout)
        self.assertEqual(comparison["unreadable_run_records"], 5)
        self.assertEqual(session["unreadable_run_records"], 5)
        self.assertEqual(comparison["by_mode"]["normal"]["runs"], 1)
        self.assertEqual(session["run_ids"], [valid])
        self.assertEqual(self.snapshot(), before)

    def test_incomplete_failure_states_and_known_partial_unknown_group_totals(self):
        self.prices()
        complete = self.start(otel=self.otel(span()))
        self.finish(complete)
        rtk_complete = self.start("rtk", otel=self.otel(span()))
        rtk_partial = self.start("rtk", otel=self.otel(span(), span(model="unlisted-model", number=2)))
        for run_id in (rtk_complete, rtk_partial):
            self.gain(run_id)
            self.finish(run_id)
        rtk_unknown = self.start("rtk")
        unknown = self.finish(rtk_unknown)
        self.assertIsNone(unknown["cost"]["estimated_usd"])
        self.assertEqual(unknown["rtk"]["savings_estimate"]["availability"], "unknown")
        pending = self.start()
        self.assertEqual(self.record(pending)["state"], "incomplete")
        self.assertIn("state=incomplete", self.cli("launch", "--run-id", pending).stdout)
        failed = self.start()
        args = self.mod.build_parser().parse_args(["finish", "--run-id", failed, "--exit-code", "7"])
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(self.mod, "finalize", side_effect=RuntimeError("PRIVATE_EXCEPTION_DETAIL")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.cmd_finish(args), 4)
        record = self.record(failed)
        self.assertEqual(record["state"], "report-failure")
        self.assertEqual(record["state_reason"], "finalization-error:RuntimeError")
        self.assertEqual(record["exit_code"], 7)
        self.assertEqual(record["copilot_outcome"], "copilot-error")
        self.assertIsNotNone(record["finalized_at"])
        self.assertNotIn("PRIVATE_EXCEPTION_DETAIL", json.dumps(record))
        self.assertIn("state=report-failure", self.cli("launch", "--run-id", failed).stdout)
        groups = json.loads(self.cli("compare", "--format", "json").stdout)["by_mode"]
        normal, rtk = groups["normal"], groups["rtk"]
        # Fixed fixture calculation, independent of the reporter's aggregate.
        one_call_usd = (700 * .1 + 200 * .01 + 100 * .125 + 100 * .5) / 1e6
        self.assertEqual(normal["states"], {"completed": 1, "incomplete": 1, "report-failure": 1})
        self.assertEqual((normal["cost_runs_complete"], normal["cost_runs_partial"],
                          normal["cost_runs_unknown"]), (1, 0, 2))
        self.assertAlmostEqual(normal["estimated_usd_sum"], one_call_usd)
        self.assertEqual(rtk["runs"], 3)
        self.assertEqual((rtk["cost_runs_complete"], rtk["cost_runs_partial"],
                          rtk["cost_runs_unknown"]), (1, 1, 1))
        self.assertEqual((rtk["calls"], rtk["input_tokens"], rtk["output_tokens"]), (3, 3000, 300))
        self.assertAlmostEqual(rtk["estimated_usd_sum"], 2 * one_call_usd)
        self.assertEqual((rtk["rtk_commands"], rtk["rtk_avoided_tokens_est"]), (4, 1600))
        self.assertAlmostEqual(rtk["rtk_value_usd_min_sum"], 2 * 800 * .1 / 1e6)
        self.assertAlmostEqual(rtk["rtk_value_usd_max_sum"], 2 * 800 * .1 / 1e6)
        self.assertEqual((rtk["rtk_value_runs_partial"], rtk["rtk_value_runs_unknown"]), (1, 1))
        session = json.loads(self.cli("session", "--session-id", "session-a", "--format", "json").stdout)
        self.assertTrue(session["mixed_modes"])
        self.assertEqual(session["by_mode"]["normal"]["runs"], 1)
        self.assertEqual(session["by_mode"]["rtk"]["cost_runs_unknown"], 1)

    def test_native_usage_whitelist_caller_ownership_and_malformed_json(self):
        run_id = self.start()
        path = self.path(run_id) / "raw/copilot-usage.json"
        write(path, json.dumps({"totalNanoAiu": 100, "totalUserRequests": True,
            "totalApiDurationMs": -3, "modifiedFiles": ["PRIVATE_WORK_PATH"],
            "prompt": "PRIVATE_PROMPT_SENTINEL", "modelMetrics": {
                "gpt-6-luna": {"requests": {"count": 2}, "usage": {"inputTokens": 12,
                    "outputTokens": 3, "cacheReadTokens": "bad"}, "command": "PRIVATE_COMMAND_SENTINEL"}}}))
        rec = self.finish(run_id)
        use = rec["native_usage"]
        self.assertEqual(use["totalNanoAiu"], 100)
        self.assertEqual(use["availability"], "partial")
        self.assertEqual(set(use["invalid_fields"]), {
            "totalUserRequests", "totalApiDurationMs",
            "modelMetrics.<model>.usage.cacheReadTokens"})
        self.assertNotIn("totalUserRequests", use)
        self.assertNotIn("totalApiDurationMs", use)
        self.assertEqual(use["models"]["gpt-6-luna"], {"requests": 2, "inputTokens": 12, "outputTokens": 3})
        self.assertNotIn("PRIVATE_", json.dumps(rec))
        caller = self.finish(self.start(owner="caller"))
        self.assertEqual(caller["native_usage"]["availability"], "unavailable")
        invalid_cases = [
            ("syntax", "{"), ("depth", "[" * 5000 + "0" + "]" * 5000),
            ("overlong-integer", '{"totalNanoAiu": ' + "9" * 5000 + "}"),
        ] + [(repr(value), json.dumps({"totalNanoAiu": value}))
             for value in (True, -1, 1.5, "12", None, 2**53 + 1, float("nan"), float("inf"))]
        for label, malformed in invalid_cases:
            with self.subTest(invalid=label):
                run_id = self.start()
                write(self.path(run_id) / "raw/copilot-usage.json", malformed)
                rec = self.finish(run_id)
                self.assertEqual(rec["native_usage"]["availability"], "unavailable")
                self.assertEqual(rec["state"], "completed")
                if label not in ("syntax", "depth"):
                    self.assertIn("totalNanoAiu", rec["native_usage"]["invalid_fields"])
        run_id = self.start()
        write(self.path(run_id) / "raw/copilot-usage.json",
              '{"totalUserRequests": 0, "totalPremiumRequestCost": 0.001}')
        use = self.finish(run_id)["native_usage"]
        self.assertEqual(use["availability"], "available")
        self.assertEqual(use["totalUserRequests"], 0)
        self.assertEqual(use["totalPremiumRequestCost"], 0.001)
        self.assertNotIn("totalNanoAiu", use, "an absent field is unknown, not a measured zero")
        self.assertNotIn("invalid_fields", use)


class TestRTKStatisticsAndIsolation(ReportHome):
    def test_private_db_numeric_only_no_global_delta_and_model_range(self):
        self.prices()
        run_id = self.start("rtk", otel=self.otel(span(), span(model="gpt-6.1-sol", number=2)))
        self.gain(run_id)
        global_db = write(self.home / "global.db", "NEVER READ OR MODIFY")
        self.env["RTK_DB_PATH"] = str(global_db)
        rec = self.finish(run_id)
        self.assertEqual(global_db.read_text(), "NEVER READ OR MODIFY")
        self.assertEqual(rec["rtk"]["stats"]["total_saved"], 800)
        self.assertNotIn("PRIVATE_COMMAND_SENTINEL", json.dumps(rec))
        sav = rec["rtk"]["savings_estimate"]
        self.assertAlmostEqual(sav["estimated_usd_min"], 800 * .1 / 1e6)
        self.assertAlmostEqual(sav["estimated_usd_max"], 800 * 2 / 1e6)
        self.assertIn("one exposure", " ".join(sav["assumptions"]))
        self.assertIn("not a bill", " ".join(sav["assumptions"]))
        self.assertEqual(rec["rtk"]["stats"]["total_time_ms"], 400)
        self.assertEqual(rec["wall_clock_ms"], 3000, "RTK elapsed must not replace wall clock")
        self.assertIn("raw command lines", json.dumps(rec["privacy"]))

    def test_partial_model_coverage_clearly_qualifies_avoided_token_range(self):
        self.prices()
        run_id = self.start("rtk", otel=self.otel(span(), span(model="unlisted-model", number=2)))
        self.gain(run_id)
        rec = self.finish(run_id)
        self.assertEqual(rec["cost"]["availability"], "partial")
        sav = rec["rtk"]["savings_estimate"]
        self.assertEqual(sav["availability"], "partial")
        self.assertEqual(sav["rate_coverage"], {
            "model_calls": 2, "priced_calls": 1, "unpriced_calls": 1,
            "unpriced_models": ["unlisted-model"], "usage_availability": "complete"})
        self.assertIn("unlisted-model", sav["qualification"])
        self.assertIn("only the priced models", sav["qualification"])
        self.assertEqual(sav["input_rate_usd_per_1m_min"], .1)
        self.assertEqual(sav["input_rate_usd_per_1m_max"], .1)
        self.assertAlmostEqual(sav["estimated_usd_min"], 800 * .1 / 1e6)
        self.assertAlmostEqual(sav["estimated_usd_max"], 800 * .1 / 1e6)
        line = next(line for line in self.cli("launch", "--run-id", run_id).stdout.splitlines()
                    if line.startswith("Approx. value"))
        self.assertIn("partial", line)
        self.assertIn("<$0.01", line)

    def test_project_settings_and_symlinked_directory_real_git_roots_are_checked(self):
        config = '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"rtk hook copilot"}]}]}}'
        # A parent HOME is not itself a project root merely because it is an
        # ancestor. Check only known lexical/real directories and git roots.
        write(self.home / ".github/copilot/settings.json", config)
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd)
        for sub in (".github/copilot", ".claude"):
            for name in ("settings.json", "settings.local.json"):
                with self.subTest(settings=(sub, name)):
                    path = write(self.cwd / sub / name, config)
                    self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
                    path.unlink()
        for sub in (".github", ".github/copilot", ".claude"):
            with self.subTest(symlinked_config_dir=sub):
                path = self.cwd / sub
                if path.exists():
                    shutil.rmtree(path)
                target = self.home / "outside-config"
                target.mkdir(exist_ok=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target, target_is_directory=True)
                self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
                path.unlink()
        other = self.home / "real-repository"
        nested = other / "src"
        nested.mkdir(parents=True)
        (other / ".git").mkdir()
        alias = self.cwd / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        hook = write(other / ".github/copilot/settings.local.json", config)
        self.cli("preflight", "--mode", "rtk", "--cwd", alias, expected=3)
        hook.unlink()
        (other / ".rtk").mkdir()
        run_id = self.start("rtk", extra=["--project-dir=" + str(alias)])
        self.assertTrue(self.record(run_id)["rtk"]["preflight"]["project_rtk_dir"].startswith("present"))

    def test_absent_db_does_not_create_it_malformed_counts_never_trusted(self):
        run_id = self.start("rtk")
        rec = self.finish(run_id)
        self.assertEqual(rec["rtk"]["stats"]["availability"], "none-recorded")
        self.assertFalse((self.path(run_id) / "raw/rtk/history.db").exists())
        for value in (True, -1, 1.5, 2**53 + 1, float("inf")):
            with self.subTest(value=value):
                run_id = self.start("rtk")
                bad = copy.deepcopy(GAIN)
                bad["summary"]["total_commands"] = value
                self.gain(run_id, bad)
                rec = self.finish(run_id)
                self.assertEqual(rec["rtk"]["stats"]["availability"], "unavailable")
                self.assertEqual(rec["rtk"]["savings_estimate"]["availability"], "unknown")
        run_id = self.start("rtk")
        write(self.path(run_id) / "raw/rtk/history.db", "broken DB")
        self.assertEqual(self.finish(run_id)["rtk"]["stats"]["availability"], "unavailable")

    def test_external_config_refused_untrusted_project_accepted_no_config_writes(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            user_dir = Path(self.mod.rtk_user_dirs()[0])
        project = write(self.cwd / ".rtk/filters.toml", "[dangerous]\ncommand = 'false'\n")
        for name in ("config.toml", "filters.toml", "trusted_filters.json"):
            with self.subTest(name=name):
                p = write(user_dir / name, "{}")
                before = self.snapshot()
                result = self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                                  "--plugin-dir", PLUGIN, expected=3)
                self.assertIn(name, result.stderr)
                self.assertEqual(self.snapshot(), before)
                p.unlink()
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, "--plugin-dir", PLUGIN)
        self.assertTrue(project.exists())
        self.assertFalse((self.home / ".copilot/session-reports").exists())

    def test_hook_config_scan_is_bounded_and_symlink_safe(self):
        hook = self.cwd / ".github/hooks/existing.json"
        write(hook, json.dumps({"command": "rtk hook copilot"}))
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        hook.unlink()
        target = write(self.home / "unmanaged-config", '{"command":"rtk hook copilot"}')
        hook.symlink_to(target)
        # An unreadable/unbounded candidate cannot be certified RTK-free.
        with self.subTest(shape="symlink"):
            self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        hook.unlink()
        write(hook, " " * (1024 * 1024) + '{"command":"rtk hook copilot"}')
        with self.subTest(shape="oversized"):
            self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)

    def test_installed_plugins_scan_inline_custom_scripts_and_unverifiable_trees(self):
        root = self.home / ".copilot/installed-plugins"
        plugin = root / "marketplace/fixture"
        inline = json.dumps({"name": "fixture", "hooks": {"PreToolUse": [{
            "matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook copilot"}]}]}})
        layouts = [
            {"plugin.json": inline},
            {".claude-plugin/plugin.json": inline},
            {".claude-plugin/plugin.json": '{"name":"fixture","hooks":"./custom/tool.json"}',
             "custom/tool.json": '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command",'
                                 '"command":"sh ./scripts/filter.sh"}]}]}}',
             "scripts/filter.sh": "#!/bin/sh\nrtk hook copilot\n"},
        ]
        for files in layouts:
            with self.subTest(layout=sorted(files)):
                for rel, text in files.items():
                    write(plugin / rel, text)
                before = self.snapshot()
                self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                         "--plugin-dir", PLUGIN, expected=3)
                self.assertEqual(self.snapshot(), before)
                shutil.rmtree(root)
        write(plugin / ".claude-plugin/plugin.json", '{"name":"safe-fixture"}')
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, "--plugin-dir", PLUGIN)

        # A hidden/unreadable portion of the tree is never treated as absent.
        oversized = write(plugin / "asset.txt", "x" * (1024 * 1024 + 1))
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        oversized.unlink()
        deep = plugin.joinpath(*(["nested"] * 9))
        write(deep / "hook.sh", "rtk hook copilot\n")
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        shutil.rmtree(plugin / "nested")
        linked_file = plugin / "symlink.json"
        target = write(self.home / "outside-plugin.json", '{"hooks":{}}')
        linked_file.symlink_to(target)
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        linked_file.unlink()
        fifo = plugin / "fifo"
        os.mkfifo(fifo)
        try:
            self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        finally:
            fifo.unlink()
        shutil.rmtree(root)
        root.symlink_to(self.home / "missing-plugin-root", target_is_directory=True)
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        root.unlink()
        root.mkdir()
        with mock.patch.object(self.mod.os, "walk",
                               return_value=iter([(str(root), [], ["f%s" % i for i in range(20001)])])):
            self.assertTrue(self.mod._installed_plugin_problems(str(root)))

        def unreadable(path, onerror=None):
            onerror(PermissionError(13, "fixture unreadable", str(path)))
            return iter(())

        with mock.patch.object(self.mod.os, "walk", side_effect=unreadable):
            problems = self.mod._installed_plugin_problems(str(root))
        self.assertTrue(any("unreadable" in why for _, why in problems), problems)

    def test_missing_and_unsupported_rtk_or_plugin_refuse_before_record(self):
        self.env["TEST_RTK_VERSION"] = "9.0.0"
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        self.env.pop("TEST_RTK_VERSION")
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd,
                 "--plugin-dir", self.home / "missing", expected=3)
        self.rtk.unlink()
        self.cli("preflight", "--mode", "rtk", "--cwd", self.cwd, expected=3)
        self.assertFalse((self.home / ".copilot/session-reports").exists())


if __name__ == "__main__":
    unittest.main()
