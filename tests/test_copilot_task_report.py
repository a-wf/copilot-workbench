#!/usr/bin/env python3
"""
Focused regression tests for copilot-task-report.py, covering the reviewer
findings fixed in this pass:

  1. OTEL install-epoch gating (pre-install spans in a resumed session's
     OTEL history must never be counted).
  2. checkpoint_cursor baseline seeding (a resumed pre-install session's
     first post-install usage_checkpoint must book only the forward delta,
     not the session's lifetime cumulative total).
  3. Timestamped configured-reasoning-effort timeline (a mid-session/
     mid-batch effort change must only relabel calls that happened after
     it, not every call in the ingest batch).
  4. wall_ms -> calendar-span semantics (first/last call timestamps, not a
     sum of per-ingest local spans).
  5. events.jsonl and OTEL are independently ingestible (a missing
     events.jsonl must never suppress OTEL ingestion).

Also covers: idempotent re-ingestion (no double counting on a repeat run
with no new data, and no double counting when re-running over the same
bytes).

Self-contained: stdlib-only (unittest), no pytest dependency, no network,
no writes outside a per-test temp directory (all of the module's file
path constants are monkeypatched to point inside it).
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "bin", "copilot-task-report.py")
SCRIPT_PATH = os.path.join(REPO_ROOT, "bin", "copilot-s")
PRICING_PATH = os.path.join(REPO_ROOT, "config", "model-pricing.json")


def load_module():
    spec = importlib.util.spec_from_file_location("copilot_task_report", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def otel_span(session_id, name, start_dt, end_dt, attrs):
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()
    a = {"gen_ai.conversation.id": session_id}
    a.update(attrs)
    return {
        "type": "span",
        "name": name,
        "startTime": [int(start_ts), int((start_ts % 1) * 1e9)],
        "endTime": [int(end_ts), int((end_ts % 1) * 1e9)],
        "attributes": a,
    }


def usage_attrs(model, prompt=100, completion=50, reasoning=0, cache_read=0, cache_write=0, level=None):
    a = {
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": prompt,
        "gen_ai.usage.output_tokens": completion,
        "gen_ai.usage.reasoning.output_tokens": reasoning,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "gen_ai.usage.cache_write.input_tokens": cache_write,
    }
    if level:
        a["gen_ai.request.reasoning.level"] = level
    return a


class BaseTestCase(unittest.TestCase):
    """Sets up an isolated fake ~/.copilot + support dir per test by
    monkeypatching the module's path constants (never touches the real
    home directory), and a fixed install epoch."""

    PRICING_FIXTURE = {
        "updated_at": "2026-01-01",
        "models": {
            "gpt-5.4": {"input_per_million": 1.75, "output_per_million": 10.00, "cache_read_per_million": 0.44},
            "claude-sonnet-5": {"input_per_million": 3.00, "output_per_million": 15.00, "cache_read_per_million": 0.30},
            "claude-opus-4-8": {"input_per_million": 15.00, "output_per_million": 75.00, "cache_read_per_million": 1.50},
        },
        "aliases": {"auto": None},
    }

    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp(prefix="copilot-task-report-test-")

        support_dir = os.path.join(self.tmp, "task-reports")
        self.mod.SUPPORT_DIR = support_dir
        self.mod.TASKS_DIR = os.path.join(support_dir, "tasks")
        self.mod.STATE_FILE = os.path.join(support_dir, "session-state.json")
        self.mod.INSTALL_MARKER_FILE = os.path.join(support_dir, "install-marker.json")
        self.mod.PRICING_FILE = os.path.join(support_dir, "model-pricing.json")
        self.mod.LOCK_FILE = os.path.join(support_dir, ".ingest.lock")
        self.mod.REPORTS_DIR = os.path.join(self.tmp, "Reports")
        self.mod.SESSION_STATE_DIR = os.path.join(self.tmp, "session-state")
        self.mod.OTEL_DIR = os.path.join(self.tmp, "otel")
        self.mod.ensure_dirs()

        # Hermetic pricing fixture: tests must not depend on the user's
        # real pricing file. A separate test verifies the shipped JSON parses.
        self.mod.atomic_write(self.mod.PRICING_FILE, json.dumps(self.PRICING_FIXTURE, indent=2))

        # Fixed install moment: T0. Tests place events before/after this.
        self.t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        marker = {"installed_at": iso(self.t0), "installed_epoch": self.t0.timestamp()}
        self.mod.atomic_write(self.mod.INSTALL_MARKER_FILE, json.dumps(marker, indent=2))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def otel_path(self, name="copilot-otel-test.jsonl"):
        return os.path.join(self.mod.OTEL_DIR, name)

    def events_path(self, session_id):
        return os.path.join(self.mod.SESSION_STATE_DIR, session_id, "events.jsonl")

    def ingest(self, session_id, task="TEST-1"):
        class Args:
            pass
        args = Args()
        args.session_id = session_id
        args.session_dir = os.path.join(self.mod.SESSION_STATE_DIR, session_id)
        args.task = task
        return self.mod.cmd_ingest(args)

    def task(self, task="TEST-1"):
        return self.mod.load_json(self.mod.task_path(task), {})


class TestOtelInstallEpochGating(BaseTestCase):
    """Finding 1 (HIGH): pre-install OTEL spans for a resumed session must
    never be counted, even though the file offset starts unseeded."""

    def test_pre_install_spans_excluded_post_install_spans_included(self):
        session_id = "sess-resumed-1"
        pre = self.t0 - timedelta(days=2)
        post = self.t0 + timedelta(minutes=5)

        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", pre, pre + timedelta(seconds=10),
                      usage_attrs("claude-sonnet-5", prompt=999999, completion=999999)),
            otel_span(session_id, "chat claude-sonnet-5", post, post + timedelta(seconds=3),
                      usage_attrs("claude-sonnet-5", prompt=100, completion=50)),
        ])

        self.ingest(session_id)
        t = self.task()
        totals = t["totals"]
        self.assertEqual(totals["call_count"], 1, "must not count the pre-install span")
        self.assertEqual(totals["prompt_tokens"], 100)
        self.assertEqual(totals["completion_tokens"], 50)

    def test_seeded_offset_skips_large_pre_install_history_efficiently(self):
        # Many pre-install spans + one post-install span: offset seeding
        # should let us verify (indirectly) that ingestion completes and
        # only counts the one post-install call, not the whole history.
        session_id = "sess-resumed-2"
        pre_base = self.t0 - timedelta(days=30)
        records = []
        for i in range(200):
            s = pre_base + timedelta(minutes=i)
            records.append(otel_span(session_id, "chat gpt-5.4", s, s + timedelta(seconds=1),
                                      usage_attrs("gpt-5.4", prompt=10, completion=10)))
        post = self.t0 + timedelta(seconds=1)
        records.append(otel_span(session_id, "chat gpt-5.4", post, post + timedelta(seconds=1),
                                  usage_attrs("gpt-5.4", prompt=7, completion=3)))
        write_jsonl(self.otel_path(), records)

        self.ingest(session_id)
        t = self.task()
        totals = t["totals"]
        self.assertEqual(totals["call_count"], 1)
        self.assertEqual(totals["prompt_tokens"], 7)
        self.assertEqual(totals["completion_tokens"], 3)

    def test_brand_new_post_install_session_counts_everything(self):
        session_id = "sess-new-1"
        s1 = self.t0 + timedelta(minutes=1)
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", s1, s1 + timedelta(seconds=2),
                      usage_attrs("claude-sonnet-5", prompt=10, completion=20)),
            otel_span(session_id, "chat claude-sonnet-5", s2, s2 + timedelta(seconds=2),
                      usage_attrs("claude-sonnet-5", prompt=30, completion=40)),
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 2)
        self.assertEqual(totals["prompt_tokens"], 40)
        self.assertEqual(totals["completion_tokens"], 60)


class TestCheckpointBaselineSeeding(BaseTestCase):
    """Finding 2 (HIGH): checkpoint_cursor must be seeded from the last
    pre-install cumulative checkpoint, so the first post-install checkpoint
    only books its forward delta, never the lifetime total."""

    def test_resumed_session_first_checkpoint_books_delta_not_lifetime_total(self):
        session_id = "sess-resumed-checkpoint"
        pre_ts = self.t0 - timedelta(days=1)
        post_ts = self.t0 + timedelta(minutes=1)

        write_jsonl(self.events_path(session_id), [
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(pre_ts),
                "data": {"totalNanoAiu": 5_000_000_000, "totalPremiumRequests": 500},
            },
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(post_ts),
                "data": {"totalNanoAiu": 5_000_050_000, "totalPremiumRequests": 503},
            },
        ])

        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["nano_aiu"], 50_000,
                          "must book only the forward delta since install, not the lifetime cumulative")
        self.assertEqual(totals["premium_requests"], 3)

    def test_brand_new_session_first_checkpoint_books_full_value(self):
        session_id = "sess-new-checkpoint"
        post_ts = self.t0 + timedelta(minutes=1)
        write_jsonl(self.events_path(session_id), [
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(post_ts),
                "data": {"totalNanoAiu": 12_345, "totalPremiumRequests": 2},
            },
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["nano_aiu"], 12_345)
        self.assertEqual(totals["premium_requests"], 2)


class TestEffortTimeline(BaseTestCase):
    """Finding 3 (MEDIUM): configured effort must be attributed per-call by
    timestamp, not "whatever the last config event in the batch says"."""

    def test_mid_batch_effort_change_only_relabels_later_calls(self):
        session_id = "sess-effort-change"
        t_start = self.t0 + timedelta(minutes=1)
        t_change = self.t0 + timedelta(minutes=5)
        t_call_before = self.t0 + timedelta(minutes=2)
        t_call_after = self.t0 + timedelta(minutes=6)

        write_jsonl(self.events_path(session_id), [
            {"type": "session.start", "timestamp": iso(t_start), "data": {"reasoningEffort": "low"}},
            {"type": "session.model_change", "timestamp": iso(t_change), "data": {"reasoningEffort": "max"}},
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call_before, t_call_before + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=1, completion=1)),
            otel_span(session_id, "chat claude-sonnet-5", t_call_after, t_call_after + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=2, completion=2)),
        ])

        # NOTE: events.jsonl is processed before OTEL within one ingest, so
        # both config events are already in the timeline by the time either
        # call is attributed. The fix is that attribution is done PER CALL
        # TIMESTAMP, not by "whatever config is current after processing
        # events" (which would mislabel both calls as "max").
        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("configured:low", by_effort)
        self.assertEqual(by_effort["configured:low"]["prompt_tokens"], 1)
        self.assertIn("configured:max", by_effort)
        self.assertEqual(by_effort["configured:max"]["prompt_tokens"], 2)
        self.assertNotIn("configured:low", by_effort.get("configured:max", {}))

    def test_pre_install_effort_config_seeded_for_early_post_install_call(self):
        session_id = "sess-effort-seeded"
        t_start = self.t0 - timedelta(days=1)  # configured before install
        t_call = self.t0 + timedelta(seconds=30)  # call shortly after install

        write_jsonl(self.events_path(session_id), [
            {"type": "session.start", "timestamp": iso(t_start), "data": {"reasoningEffort": "high"}},
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=5, completion=5)),
        ])

        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("configured:high", by_effort,
                      "pre-install effort config must still be usable to attribute an early post-install call")
        self.assertNotIn("unknown", by_effort)

    def test_measured_effort_on_span_wins_over_configured(self):
        session_id = "sess-effort-measured"
        t_start = self.t0 + timedelta(minutes=1)
        t_call = self.t0 + timedelta(minutes=2)
        write_jsonl(self.events_path(session_id), [
            {"type": "session.start", "timestamp": iso(t_start), "data": {"reasoningEffort": "low"}},
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=1, completion=1, level="max")),
        ])
        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("measured:max", by_effort)
        self.assertNotIn("configured:low", by_effort)

    def test_configured_effort_wins_over_inferred_inside_closed_coder_interval(self):
        """Priority order per the module docstring/spec is measured >
        configured > inferred > unknown. A call with no per-call measured
        level, whose timestamp falls inside a fully-closed 'coder' subagent
        interval (which would map to "inferred:high" via
        CUSTOM_AGENT_EFFORT_MAP), must still be labeled from the configured
        timeline ("configured:low") when a configured entry applies at that
        timestamp — configured must never be shadowed by inferred."""
        session_id = "sess-configured-beats-inferred"
        t_start = self.t0 + timedelta(minutes=1)
        t_agent_start = self.t0 + timedelta(minutes=2)
        t_call = self.t0 + timedelta(minutes=3)  # inside the coder interval
        t_agent_end = self.t0 + timedelta(minutes=4)

        write_jsonl(self.events_path(session_id), [
            {"type": "session.start", "timestamp": iso(t_start), "data": {"reasoningEffort": "low"}},
            {
                "type": "subagent.started",
                "agentId": "agent-1",
                "timestamp": iso(t_agent_start),
                "data": {"agentName": "coder"},
            },
            {
                "type": "subagent.completed",
                "agentId": "agent-1",
                "timestamp": iso(t_agent_end),
                "data": {"agentName": "coder", "totalTokens": 10, "durationMs": 1000},
            },
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=7, completion=3)),
        ])

        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("configured:low", by_effort,
                      "a configured timeline entry must win over an inferred subagent-role guess")
        self.assertEqual(by_effort["configured:low"]["prompt_tokens"], 7)
        self.assertNotIn("inferred:high", by_effort)


class TestCalendarSpan(BaseTestCase):
    """Finding 4 (LOW): calendar span must be earliest->latest call
    timestamp across all ingests, not a sum of per-ingest local spans."""

    def test_span_is_earliest_to_latest_not_summed_across_ingests(self):
        session_id = "sess-span"
        c1_start = self.t0 + timedelta(minutes=0)
        c1_end = self.t0 + timedelta(minutes=0, seconds=2)
        c2_start = self.t0 + timedelta(minutes=10)
        c2_end = self.t0 + timedelta(minutes=10, seconds=2)

        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", c1_start, c1_end, usage_attrs("gpt-5.4")),
        ])
        self.ingest(session_id)

        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", c2_start, c2_end, usage_attrs("gpt-5.4")),
        ])
        self.ingest(session_id)

        totals = self.task()["totals"]
        span_s = totals["last_call_ts"] - totals["first_call_ts"]
        # True calendar span: from c1_start to c2_end == 10 minutes 2 seconds.
        self.assertAlmostEqual(span_s, timedelta(minutes=10, seconds=2).total_seconds(), delta=1)
        # Sanity: NOT the (wrong) sum of the two tiny 2-second local spans.
        self.assertGreater(span_s, 4)


class TestNoOpGuardCoversAllMergeableFields(BaseTestCase):
    """Finding 2: cmd_ingest's "nothing to merge, just persist offsets"
    no-op guard must check every mergeable delta field. Before the fix it
    only checked model_calls/nano_aiu_delta/agent_summaries, so a
    premium-requests-only or repository-only update (no new model calls,
    no nano-AIU, no agent summaries) was silently dropped from the ticket
    even though the underlying byte offsets had already advanced past the
    data that produced it — making it unrecoverable on a later ingest."""

    def test_premium_requests_only_update_is_not_dropped(self):
        session_id = "sess-premium-only"
        checkpoint_ts = self.t0 + timedelta(minutes=1)
        write_jsonl(self.events_path(session_id), [
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(checkpoint_ts),
                # nano AIU delta is zero; only premium_requests moves.
                "data": {"totalNanoAiu": 0, "totalPremiumRequests": 3},
            },
        ])
        # Deliberately no OTEL file at all: model_calls stays 0.

        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals.get("premium_requests"), 3,
                          "a premium-only checkpoint delta must still be merged into the ticket")
        self.assertEqual(totals.get("nano_aiu", 0), 0)
        self.assertEqual(totals.get("call_count", 0), 0)

    def test_repository_only_update_is_not_dropped(self):
        session_id = "sess-repo-only"
        t_start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.events_path(session_id), [
            # No reasoningEffort, no checkpoint, no subagent events: only a
            # repository context is captured from this session.start.
            {"type": "session.start", "timestamp": iso(t_start), "data": {"context": {"repository": "org/repo-a"}}},
        ])
        # Deliberately no OTEL file at all: model_calls stays 0.

        self.ingest(session_id)
        ticket = self.task()
        self.assertEqual(ticket.get("repositories"), ["org/repo-a"],
                          "a repository-only update must still be merged into the ticket")


class TestIndependentIngestion(BaseTestCase):
    """Finding 5 (LOW): a missing events.jsonl must not suppress OTEL
    ingestion; each source is independently ingestible."""

    def test_otel_ingested_with_no_events_jsonl_at_all(self):
        session_id = "sess-no-events"
        # Deliberately do NOT create events.jsonl / its parent dir.
        s = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", s, s + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=42, completion=8)),
        ])
        self.assertFalse(os.path.exists(self.events_path(session_id)))

        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 1)
        self.assertEqual(totals["prompt_tokens"], 42)
        self.assertEqual(totals["completion_tokens"], 8)

    def test_events_jsonl_appearing_later_is_picked_up_next_ingest(self):
        session_id = "sess-events-appears-later"
        s1 = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", s1, s1 + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=1, completion=1)),
        ])
        self.ingest(session_id)  # no events.jsonl yet

        checkpoint_ts = self.t0 + timedelta(minutes=2)
        write_jsonl(self.events_path(session_id), [
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(checkpoint_ts),
                "data": {"totalNanoAiu": 999, "totalPremiumRequests": 1},
            },
        ])
        self.ingest(session_id)  # events.jsonl exists now

        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 1)
        self.assertEqual(totals["nano_aiu"], 999)
        self.assertEqual(totals["premium_requests"], 1)


class TestIdempotence(BaseTestCase):
    """Re-running ingest with no new bytes must never double count, and a
    resumed-session backfill must remain excluded on repeated ingests too."""

    def test_reingest_with_no_new_data_does_not_double_count(self):
        session_id = "sess-idempotent"
        s = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", s, s + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=10, completion=5)),
        ])
        write_jsonl(self.events_path(session_id), [
            {
                "type": "session.usage_checkpoint",
                "timestamp": iso(s),
                "data": {"totalNanoAiu": 100, "totalPremiumRequests": 1},
            },
        ])

        self.ingest(session_id)
        self.ingest(session_id)
        self.ingest(session_id)

        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 1)
        self.assertEqual(totals["prompt_tokens"], 10)
        self.assertEqual(totals["nano_aiu"], 100)
        self.assertEqual(totals["premium_requests"], 1)

    def test_resumed_pre_install_backfill_stays_excluded_across_reingests(self):
        session_id = "sess-idempotent-resumed"
        pre = self.t0 - timedelta(days=5)
        post = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", pre, pre + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=999, completion=999)),
        ])
        self.ingest(session_id)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", post, post + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=3, completion=4)),
        ])
        self.ingest(session_id)
        self.ingest(session_id)  # extra no-op re-ingest

        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 1)
        self.assertEqual(totals["prompt_tokens"], 3)
        self.assertEqual(totals["completion_tokens"], 4)

    def test_new_chat_calls_after_reingest_add_correctly(self):
        session_id = "sess-idempotent-append"
        s1 = self.t0 + timedelta(minutes=1)
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=1, completion=1)),
        ])
        self.ingest(session_id)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=2, completion=2)),
        ])
        self.ingest(session_id)

        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 2)
        self.assertEqual(totals["prompt_tokens"], 3)
        self.assertEqual(totals["completion_tokens"], 3)


class TestCmdReportKeyValidation(BaseTestCase):
    """Finding 3: cmd_report must strictly normalize/validate the task ID
    via normalize_task_id(), never falling back to a raw
    strip()+upper() of caller input for a key that doesn't actually match
    the task ID format — a malformed ID must fail clearly instead of
    being looked up (or silently creating a ticket/report file) under
    whatever garbage string was passed in."""

    def report(self, task_id):
        class Args:
            pass
        args = Args()
        args.task_id = task_id
        return self.mod.cmd_report(args)

    def test_malformed_key_fails_clearly_instead_of_raw_fallback(self):
        rc = self.report("not a valid key!!")
        self.assertEqual(rc, 1)
        # Must not have created/looked up a ticket/report file under the
        # raw, merely-uppercased garbage string.
        raw_upper = "NOT A VALID KEY!!"
        self.assertFalse(os.path.exists(self.mod.task_path(raw_upper)))
        self.assertFalse(os.path.exists(self.mod.report_path(raw_upper)))

    def test_valid_key_is_normalized_and_reported(self):
        # Seed a ticket the same way ingest would (already-normalized key).
        ticket_dir_key = "ABC-123"
        self.mod.atomic_write(
            self.mod.task_path(ticket_dir_key),
            json.dumps({"task_id": ticket_dir_key, "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
        )
        # Lowercase input with incidental whitespace must still resolve to
        # the same normalized ticket.
        rc = self.report("  abc-123  ")
        self.assertEqual(rc, 0)

    def test_unassigned_literal_is_accepted(self):
        self.mod.atomic_write(
            self.mod.task_path(self.mod.UNASSIGNED),
            json.dumps({"task_id": self.mod.UNASSIGNED, "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
        )
        rc = self.report("unassigned")
        self.assertEqual(rc, 0)


class TestInstallMarkerFutureOnly(BaseTestCase):
    """Install marker must represent the moment the feature was installed,
    not some historical epoch, so pre-marker telemetry is excluded."""

    def test_ensure_marker_uses_current_epoch_when_missing(self):
        # Remove the fixed-epoch marker created by BaseTestCase so we can
        # verify a brand-new marker uses the current time.
        os.remove(self.mod.INSTALL_MARKER_FILE)
        before = datetime.now(timezone.utc).timestamp()
        rc = self.mod.cmd_ensure_marker(type("Args", (), {})())
        after = datetime.now(timezone.utc).timestamp()
        self.assertEqual(rc, 0)
        marker = self.mod.load_json(self.mod.INSTALL_MARKER_FILE, {})
        self.assertIn("installed_epoch", marker)
        self.assertTrue(before <= marker["installed_epoch"] <= after)

    def test_events_before_marker_are_excluded_after_included(self):
        # Replace the fixed-epoch marker with a fresh one so "pre-install"
        # is relative to the real install moment.
        os.remove(self.mod.INSTALL_MARKER_FILE)
        self.mod.cmd_ensure_marker(type("Args", (), {})())
        install_epoch = self.mod.load_json(self.mod.INSTALL_MARKER_FILE, {})["installed_epoch"]
        session_id = "sess-marker-future"
        pre = datetime.fromtimestamp(install_epoch - 300, tz=timezone.utc)
        post = datetime.fromtimestamp(install_epoch + 300, tz=timezone.utc)
        write_jsonl(self.events_path(session_id), [
            {"type": "session.usage_checkpoint", "timestamp": iso(pre),
             "data": {"totalNanoAiu": 9_999_999, "totalPremiumRequests": 99}},
            {"type": "session.usage_checkpoint", "timestamp": iso(post),
             "data": {"totalNanoAiu": 10_000_100, "totalPremiumRequests": 100}},
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["nano_aiu"], 101)
        self.assertEqual(totals["premium_requests"], 1)


class TestOtelSessionFilteringAndIncremental(BaseTestCase):
    """OTEL spans must be filtered by conversation id, and ingestion must
    be incremental (new bytes only, no double counting)."""

    def test_only_spans_for_target_session_are_counted(self):
        s = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span("sess-a", "chat gpt-5.4", s, s + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=10, completion=5)),
            otel_span("sess-b", "chat gpt-5.4", s, s + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=20, completion=10)),
        ])
        self.ingest("sess-a", "AA-1")
        self.ingest("sess-b", "BB-1")
        self.assertEqual(self.task("AA-1")["totals"]["prompt_tokens"], 10)
        self.assertEqual(self.task("BB-1")["totals"]["prompt_tokens"], 20)

    def test_appended_spans_are_counted_without_recounting_old_ones(self):
        session_id = "sess-incremental"
        s1 = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=1, completion=1)),
        ])
        self.ingest(session_id)
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=2, completion=2)),
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["call_count"], 2)
        self.assertEqual(totals["prompt_tokens"], 3)
        self.assertEqual(totals["completion_tokens"], 3)


class TestMultiSessionSameTicketAggregation(BaseTestCase):
    """Multiple sessions may contribute to the same task."""

    def test_two_sessions_aggregate_into_same_ticket(self):
        s1 = self.t0 + timedelta(minutes=1)
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span("sess-1", "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=10, completion=5)),
            otel_span("sess-2", "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=20, completion=10)),
        ])
        self.ingest("sess-1", "AGG-1")
        self.ingest("sess-2", "AGG-1")
        t = self.task("AGG-1")
        self.assertEqual(sorted(t["sessions"]), ["sess-1", "sess-2"])
        self.assertEqual(t["totals"]["call_count"], 2)
        self.assertEqual(t["totals"]["prompt_tokens"], 30)
        self.assertEqual(t["totals"]["completion_tokens"], 15)


class TestReportRendering(BaseTestCase):
    """cmd_ingest must write a Markdown report with meaningful rendered
    values, and cmd_report must print that report (not just return 0)."""

    def _seed_report_data(self, session_id, task):
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t, t + timedelta(seconds=2),
                      usage_attrs("claude-sonnet-5", prompt=1000, completion=500,
                                  reasoning=50, cache_read=200, level="high")),
        ])
        write_jsonl(self.events_path(session_id), [
            {"type": "session.usage_checkpoint", "timestamp": iso(t),
             "data": {"totalNanoAiu": 1_000_000_000, "totalPremiumRequests": 10}},
        ])
        self.assertEqual(self.ingest(session_id, task), 0)

    def test_cmd_ingest_writes_markdown_report(self):
        session_id = "sess-report"
        task = "REPORT-1"
        self._seed_report_data(session_id, task)
        rpath = self.mod.report_path(task)
        self.assertTrue(os.path.exists(rpath), "Markdown report must be written after ingest")
        with open(rpath, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("# Copilot Task Usage Report — %s" % task, md)
        self.assertIn("Model calls (from OTEL chat spans", md)
        self.assertIn("| 1 |", md)  # one call in summary/model row
        self.assertIn("| 1000 |", md)  # prompt tokens
        self.assertIn("| 500 |", md)  # completion tokens
        self.assertIn("claude-sonnet-5", md)
        self.assertIn("measured:high", md)
        self.assertIn("Model-call time", md)
        self.assertIn("2.0s", md)
        self.assertIn("$0.0100", md)  # fixture pricing: (800*3 + 200*0.3 + 500*15)/1e6
        self.assertIn("1.000000 AIU", md)

    def test_cmd_report_outputs_rendered_content(self):
        session_id = "sess-report-cmd"
        task = "REPORT-2"
        self._seed_report_data(session_id, task)
        class Args:
            pass
        args = Args()
        args.task_id = task
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = self.mod.cmd_report(args)
        self.assertEqual(rc, 0)
        md = out_buf.getvalue()
        self.assertIn("# Copilot Task Usage Report — %s" % task, md)
        self.assertIn("claude-sonnet-5", md)
        self.assertIn("measured:high", md)
        self.assertIn("$0.0100", md)
        self.assertIn("(report file:", err_buf.getvalue())


class TestUnassignedAndInvalidTaskFallback(BaseTestCase):
    """Explicit UNASSIGNED ingest and invalid --task must fall back safely
    to the UNASSIGNED bucket rather than creating a malformed task file."""

    def test_unassigned_ingest_creates_unassigned_ticket(self):
        session_id = "sess-unassigned"
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", t, t + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=7, completion=3)),
        ])
        self.assertEqual(self.ingest(session_id, None), 0)
        t = self.task(self.mod.UNASSIGNED)
        self.assertEqual(t["task_id"], self.mod.UNASSIGNED)
        self.assertEqual(t["totals"]["call_count"], 1)
        self.assertTrue(os.path.exists(self.mod.report_path(self.mod.UNASSIGNED)))

    def test_invalid_task_fallback_to_unassigned(self):
        session_id = "sess-invalid"
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", t, t + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=5, completion=2)),
        ])
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            rc = self.ingest(session_id, "not-a-key!!")
        self.assertEqual(rc, 0)
        self.assertIn("ignoring invalid task ID", stderr_capture.getvalue())
        self.assertFalse(os.path.exists(self.mod.task_path("NOT-A-KEY!!")))
        self.assertFalse(os.path.exists(self.mod.task_path("not-a-key!!")))
        t = self.task(self.mod.UNASSIGNED)
        self.assertEqual(t["task_id"], self.mod.UNASSIGNED)
        self.assertEqual(t["totals"]["call_count"], 1)


class TestReportPersistenceAfterDeletion(BaseTestCase):
    """Deleting a session directory must not remove the already-ingested
    ticket/report; a later session for the same ticket must still aggregate
    on top of the persisted data."""

    def test_ticket_and_report_persist_after_session_dir_deleted(self):
        task = "PERSIST-1"
        s1 = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span("sess-persist-a", "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=10, completion=5)),
        ])
        self.ingest("sess-persist-a", task)
        self.assertTrue(os.path.exists(self.mod.task_path(task)))
        self.assertTrue(os.path.exists(self.mod.report_path(task)))

        # Simulate the user deleting the session state directory.
        shutil.rmtree(os.path.join(self.mod.SESSION_STATE_DIR, "sess-persist-a"), ignore_errors=True)
        self.assertFalse(os.path.exists(self.events_path("sess-persist-a")))

        # Ticket/report must still be present.
        t = self.task(task)
        self.assertIn("sess-persist-a", t["sessions"])
        self.assertEqual(t["totals"]["call_count"], 1)
        self.assertTrue(os.path.exists(self.mod.report_path(task)))

        # A new session for the same ticket aggregates on top.
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span("sess-persist-b", "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=20, completion=10)),
        ])
        self.ingest("sess-persist-b", task)
        t = self.task(task)
        self.assertEqual(sorted(t["sessions"]), ["sess-persist-a", "sess-persist-b"])
        self.assertEqual(t["totals"]["call_count"], 2)
        self.assertEqual(t["totals"]["prompt_tokens"], 30)
        self.assertEqual(t["totals"]["completion_tokens"], 15)


class TestShippedPricingParses(unittest.TestCase):
    """The pricing JSON shipped in this repo (config/model-pricing.json)
    must be valid JSON and contain the expected top-level keys. This is a
    read-only sanity check and is separate from the hermetic fixture used
    by all other tests."""

    def test_shipped_pricing_json_is_valid(self):
        self.assertTrue(os.path.exists(PRICING_PATH), "shipped pricing file missing")
        with open(PRICING_PATH, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("models", data)
        self.assertIn("aliases", data)
        self.assertIsInstance(data["models"], dict)
        self.assertIsInstance(data["aliases"], dict)


class TestInstalledArtifactsOptional(unittest.TestCase):
    """Optional, best-effort smoke checks against a *live installation* on
    this machine (~/.copilot/task-reports/model-pricing.json and
    ~/.local/bin/copilot-s). These are separate from the hermetic repo
    checks above: they never assume the toolkit has been installed, and
    they skip cleanly (rather than fail) when it hasn't."""

    def test_installed_pricing_json_if_present(self):
        installed_pricing = os.path.join(
            os.path.expanduser("~"), ".copilot", "task-reports", "model-pricing.json"
        )
        if not os.path.exists(installed_pricing):
            self.skipTest("no installed pricing file on this machine")
        with open(installed_pricing, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("models", data)
        self.assertIn("aliases", data)

    def test_installed_copilot_s_if_present(self):
        installed_script = os.path.join(os.path.expanduser("~"), ".local", "bin", "copilot-s")
        if not os.path.exists(installed_script):
            self.skipTest("no installed copilot-s on this machine")
        self.assertTrue(os.access(installed_script, os.X_OK), "installed copilot-s is not executable")


class TestPricingAliasesAndUnknownModels(BaseTestCase):
    """Pricing must resolve aliases, treat explicitly-unpriced aliases as
    unknown, and report unknown models instead of silently pricing at $0."""

    def test_alias_unknown_and_unpriced_models(self):
        s = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span("sess", "chat claude-opus-4-8", s, s + timedelta(seconds=1),
                      usage_attrs("claude-opus-4-8", prompt=1_000_000, completion=1_000_000)),
            otel_span("sess", "chat unknown-model", s + timedelta(seconds=2), s + timedelta(seconds=3),
                      usage_attrs("unknown-model", prompt=100, completion=50)),
            otel_span("sess", "chat auto", s + timedelta(seconds=4), s + timedelta(seconds=5),
                      usage_attrs("auto", prompt=100, completion=50)),
        ])
        self.ingest("sess", "PRICE-1")
        t = self.task("PRICE-1")
        self.assertIn("claude-opus-4-8", t["by_model"])
        self.assertIn("unknown-model", t["by_model"])
        self.assertIn("auto", t["by_model"])
        total_usd, priced, unpriced = self.mod.estimate_usd(t["by_model"])
        self.assertIsNotNone(total_usd)
        self.assertIn("unknown-model", unpriced)
        self.assertIn("auto", unpriced)
        # claude-opus-4.8: $15/M input, $75/M output; 1M each -> $90.
        self.assertAlmostEqual(total_usd, 90.0, places=4)


class TestCacheAndReasoningCostMath(BaseTestCase):
    """Reasoning tokens are a subset of completion tokens (not added).
    Cache-read tokens are a subset of input tokens priced separately."""

    def test_reasoning_not_added_and_cache_read_priced_separately(self):
        agg = {
            "prompt_tokens": 1000, "completion_tokens": 500,
            "reasoning_tokens": 200, "cache_read_tokens": 800,
            "cache_write_tokens": 0, "total_tokens": 1500,
            "call_count": 1, "duration_ms": 0,
        }
        price = {"input_per_million": 1.0, "output_per_million": 2.0,
                 "cache_read_per_million": 0.1}
        cost = self.mod.call_cost(agg, price)
        expected = (200 * 1.0 + 800 * 0.1 + 500 * 2.0) / 1_000_000.0
        self.assertAlmostEqual(cost, expected)
        self.assertAlmostEqual(cost, 0.00128)

    def test_cache_read_capped_at_prompt_tokens(self):
        agg = {
            "prompt_tokens": 100, "completion_tokens": 0,
            "reasoning_tokens": 0, "cache_read_tokens": 200,
            "cache_write_tokens": 0, "total_tokens": 100,
            "call_count": 1, "duration_ms": 0,
        }
        price = {"input_per_million": 1.0, "output_per_million": 0,
                 "cache_read_per_million": 0.5}
        cost = self.mod.call_cost(agg, price)
        self.assertAlmostEqual(cost, (100 * 0.5) / 1_000_000.0)

    def test_cache_read_falls_back_to_input_rate(self):
        agg = {
            "prompt_tokens": 1000, "completion_tokens": 0,
            "reasoning_tokens": 0, "cache_read_tokens": 500,
            "cache_write_tokens": 0, "total_tokens": 1000,
            "call_count": 1, "duration_ms": 0,
        }
        price = {"input_per_million": 2.0, "output_per_million": 0}
        cost = self.mod.call_cost(agg, price)
        self.assertAlmostEqual(cost, (1000 * 2.0) / 1_000_000.0)

    def test_cache_read_explicit_null_falls_back_to_input_rate(self):
        # A pricing entry with `"cache_read_per_million": null` (i.e. the
        # key IS present but its value is None) must fall back to
        # input_per_million exactly like the key being absent entirely —
        # not be passed through as None and blow up the multiplication.
        agg = {
            "prompt_tokens": 1000, "completion_tokens": 0,
            "reasoning_tokens": 0, "cache_read_tokens": 500,
            "cache_write_tokens": 0, "total_tokens": 1000,
            "call_count": 1, "duration_ms": 0,
        }
        price = {"input_per_million": 2.0, "output_per_million": 0,
                  "cache_read_per_million": None}
        cost = self.mod.call_cost(agg, price)
        self.assertAlmostEqual(cost, (1000 * 2.0) / 1_000_000.0)


class TestEffortPrecedencePositiveInferred(BaseTestCase):
    """measured > configured > inferred > unknown."""

    def test_inferred_effort_when_no_configured_effort(self):
        session_id = "sess-inferred"
        t_start = self.t0 + timedelta(minutes=1)
        t_call = self.t0 + timedelta(minutes=2)
        t_end = self.t0 + timedelta(minutes=3)
        write_jsonl(self.events_path(session_id), [
            {"type": "subagent.started", "agentId": "a1", "timestamp": iso(t_start),
             "data": {"agentName": "tester"}},
            {"type": "subagent.completed", "agentId": "a1", "timestamp": iso(t_end),
             "data": {"agentName": "tester", "totalTokens": 10, "durationMs": 1000}},
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=7, completion=3)),
        ])
        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("inferred:medium", by_effort)
        self.assertEqual(by_effort["inferred:medium"]["prompt_tokens"], 7)
        self.assertNotIn("configured:medium", by_effort)

    def test_unknown_when_no_effort_info(self):
        session_id = "sess-unknown"
        t_call = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=1, completion=1)),
        ])
        self.ingest(session_id)
        self.assertIn("unknown", self.task()["by_effort"])


class TestCalendarVsActiveTime(BaseTestCase):
    """Calendar span uses earliest/latest timestamps; active model-call
    time sums individual span durations."""

    def test_active_time_sums_durations_calendar_uses_timestamps(self):
        session_id = "sess-time"
        c1_start = self.t0 + timedelta(minutes=0)
        c1_end = c1_start + timedelta(seconds=2)
        c2_start = self.t0 + timedelta(minutes=10)
        c2_end = c2_start + timedelta(seconds=3)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", c1_start, c1_end, usage_attrs("gpt-5.4")),
            otel_span(session_id, "chat gpt-5.4", c2_start, c2_end, usage_attrs("gpt-5.4")),
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["duration_ms"], 5000)
        span_s = totals["last_call_ts"] - totals["first_call_ts"]
        self.assertAlmostEqual(span_s, timedelta(minutes=10, seconds=3).total_seconds(), delta=1)


class TestMissingAndTruncatedTelemetry(BaseTestCase):
    """Missing files must not block other sources; truncation must warn and
    resync safely."""

    def test_missing_otel_does_not_block_events_only_ingest(self):
        session_id = "sess-no-otel"
        ts = self.t0 + timedelta(minutes=1)
        write_jsonl(self.events_path(session_id), [
            {"type": "session.usage_checkpoint", "timestamp": iso(ts),
             "data": {"totalNanoAiu": 123, "totalPremiumRequests": 2}},
        ])
        self.assertFalse(os.path.exists(self.otel_path()))
        self.ingest(session_id)
        totals = self.task()["totals"]
        self.assertEqual(totals["nano_aiu"], 123)
        self.assertEqual(totals["premium_requests"], 2)
        self.assertEqual(totals["call_count"], 0)

    def test_truncation_warns_and_resyncs_without_double_count(self):
        session_id = "sess-trunc"
        s1 = self.t0 + timedelta(minutes=1)
        s2 = self.t0 + timedelta(minutes=2)
        path = self.otel_path()
        write_jsonl(path, [
            otel_span(session_id, "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=10, completion=5)),
            otel_span(session_id, "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=20, completion=10)),
        ])
        self.ingest(session_id)
        self.assertEqual(self.task()["totals"]["call_count"], 2)
        # Truncate to just the first line, then ingest to resync.
        with open(path, "rb") as f:
            first_line = f.readline()
        with open(path, "wb") as f:
            f.write(first_line)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.ingest(session_id)
        # The truncated second span must not be re-counted during resync.
        self.assertEqual(self.task()["totals"]["call_count"], 2)
        # New data appended after the resync must be counted on the next ingest.
        s3 = self.t0 + timedelta(minutes=3)
        write_jsonl(path, [
            otel_span(session_id, "chat gpt-5.4", s3, s3 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=30, completion=15)),
        ])
        self.ingest(session_id)
        totals = self.task()["totals"]
        # First + second counted originally; third span is new after resync.
        # Second span is gone from the file and must not be double counted.
        self.assertEqual(totals["call_count"], 3)
        self.assertEqual(totals["prompt_tokens"], 60)
        self.assertIn("shrank/rewound", stderr_capture.getvalue())


class TestConcurrentIngestNoClobber(BaseTestCase):
    """Two concurrent cmd_ingest processes must serialize on the lock and
    must not clobber or double-count the shared ticket/state files."""

    def _worker_code(self, home, session_id, ticket, index):
        start = self.t0 + timedelta(minutes=1 + index)
        span = otel_span(session_id, "chat gpt-5.4", start, start + timedelta(seconds=1),
                         usage_attrs("gpt-5.4", prompt=10 * (index + 1),
                                     completion=5 * (index + 1)))
        return (
            "import json, os, subprocess, sys\n"
            "home = %r\n"
            "module = %r\n"
            "session_id = %r\n"
            "ticket = %r\n"
            "span = %s\n"
            "otel_dir = os.path.join(home, '.copilot', 'otel')\n"
            "os.makedirs(otel_dir, exist_ok=True)\n"
            "sess_dir = os.path.join(home, '.copilot', 'session-state', session_id)\n"
            "os.makedirs(sess_dir, exist_ok=True)\n"
            "path = os.path.join(otel_dir, 'concurrent.jsonl')\n"
            "with open(path, 'a') as f:\n"
            "    f.write(json.dumps(span) + '\\n')\n"
            "subprocess.run([sys.executable, module, 'ingest',\n"
            "    '--session-id', session_id,\n"
            "    '--task', ticket,\n"
            "    '--session-dir', sess_dir], check=True)\n"
        ) % (home, MODULE_PATH, session_id, ticket, json.dumps(span))

    def test_concurrent_ingests_do_not_clobber_or_double_count(self):
        if self.mod.fcntl is None:
            self.skipTest("fcntl not available")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".copilot", "task-reports"), exist_ok=True)
        os.makedirs(os.path.join(home, ".copilot", "otel"), exist_ok=True)
        os.makedirs(os.path.join(home, ".copilot", "session-state"), exist_ok=True)
        # Install marker + pricing fixture for the subprocesses.
        marker = {"installed_at": iso(self.t0), "installed_epoch": self.t0.timestamp()}
        with open(os.path.join(home, ".copilot", "task-reports", "install-marker.json"), "w") as f:
            json.dump(marker, f)
        with open(os.path.join(home, ".copilot", "task-reports", "model-pricing.json"), "w") as f:
            json.dump(self.PRICING_FIXTURE, f)

        session_id = "sess-concurrent"
        ticket = "CONCUR-1"
        procs = []
        for i in range(2):
            code = self._worker_code(home, session_id, ticket, i)
            procs.append(subprocess.Popen([sys.executable, "-c", code],
                                          env={**os.environ, "HOME": home}))
        for p in procs:
            p.wait(timeout=30)
            self.assertEqual(p.returncode, 0)

        task_path = os.path.join(home, ".copilot", "task-reports", "tasks", "%s.json" % ticket)
        self.assertTrue(os.path.exists(task_path), "ticket file must exist after concurrent ingests")
        with open(task_path) as f:
            t = json.load(f)
        self.assertEqual(t["totals"]["call_count"], 2)
        self.assertEqual(t["totals"]["prompt_tokens"], 30)
        self.assertEqual(t["totals"]["completion_tokens"], 15)


class TestCopilotSShell(unittest.TestCase):
    """Shell-level tests for copilot-s: task-ID extraction/normalization,
    UNASSIGNED fallback, --report argument parsing, nonfatal reporting,
    and multi-delete ingest-before-delete ordering."""

    SCRIPT = SCRIPT_PATH

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="copilot-s-test-")
        self.home = self.tmp
        os.makedirs(os.path.join(self.home, ".local", "bin"), exist_ok=True)
        self._write_fake_helper()
        # Bash 3.2 on macOS does not source reliably from process
        # substitution, so create a temp copy of copilot-s with the final
        # `main "$@"` call removed and source that file instead.
        self.sourced = os.path.join(self.tmp, "copilot-s-sourced.sh")
        with open(self.sourced, "w") as out:
            subprocess.run(["sed", "$d", self.SCRIPT], stdout=out, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fake_helper(self):
        helper = os.path.join(self.home, ".local", "bin", "copilot-task-report.py")
        self.helper_path = helper
        code = '''#!/usr/bin/env python3
import os, re, sys
HOME = os.environ["HOME"]
log = os.path.join(HOME, "helper.log")
def write_log(msg):
    with open(log, "a") as f:
        f.write(msg + "\\n")
if len(sys.argv) < 2:
    sys.exit(2)
cmd = sys.argv[1]
if cmd == "ensure-marker":
    marker_path = os.path.join(HOME, ".copilot", "task-reports", "install-marker.json")
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    with open(marker_path, "w") as f:
        f.write('{"installed_at":"2026-09-21T12:00:00Z","installed_epoch":2000000000.0}')
    sys.exit(0)
if cmd == "ingest":
    session_id = ""
    task = ""
    session_dir = ""
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--session-id" and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--task" and i + 1 < len(sys.argv):
            task = sys.argv[i + 1]; i += 2
        elif sys.argv[i].startswith("--task="):
            task = sys.argv[i][len("--task="):]; i += 1
        elif sys.argv[i] == "--session-dir" and i + 1 < len(sys.argv):
            session_dir = sys.argv[i + 1]; i += 2
        else:
            i += 1
    dir_exists = os.path.isdir(session_dir) if session_dir else False
    events_path = os.path.join(session_dir, "events.jsonl") if session_dir else ""
    events_exists = os.path.isfile(events_path) if session_dir else False
    write_log(f"ingest session={session_id} task={task} dir_exists={dir_exists} events_exists={events_exists}")
    if not dir_exists:
        print(f"helper: session dir missing at ingest time: {session_dir}", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("COPILOT_TASK_REPORT_FAIL") == "1":
        print("helper forced failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if cmd == "report":
    rest = sys.argv[2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    key = rest[0] if rest else ""
    if not re.match(r'^[A-Z][A-Z0-9]+-[0-9]+$|^UNASSIGNED$', key):
        print(f"invalid key: {key}", file=sys.stderr)
        sys.exit(1)
    print(f"report for {key}")
    sys.exit(0)
sys.exit(2)
'''
        with open(helper, "w") as f:
            f.write(code)
        os.chmod(helper, 0o755)

    def _run(self, snippet, stdin_text=None, cwd=None, env=None):
        full_env = os.environ.copy()
        full_env["HOME"] = self.home
        # These tests exercise task-reporting *behavior* against a fake
        # helper, not helper-path *resolution* (covered separately in
        # tests/test_copilot_s.py) — so always point copilot-s at the fake
        # helper via the explicit override, regardless of where self.SCRIPT
        # happens to live relative to it (e.g. executed directly from the
        # real repo bin/, which has its own real sibling helper).
        full_env["COPILOT_TASK_REPORT_HELPER"] = self.helper_path
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["bash", "-c", snippet],
            input=stdin_text,
            text=True,
            capture_output=True,
            cwd=cwd or self.tmp,
            env=full_env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _source_prefix(self):
        # Source the no-main copy of copilot-s, then replace the TTY-based
        # prompt with stdin-based input so tests can drive interactive
        # functions safely.
        return (
            'source "%s"\n'
            'prompt_read() { local varname="$1"; read -r "$varname"; }\n' % self.sourced
        )

    def test_bash_syntax_is_valid(self):
        rc, out, err = self._run("bash -n '%s'" % self.SCRIPT)
        self.assertEqual(rc, 0, err)

    def test_extract_task_key_from_branch(self):
        rc, out, err = self._run(self._source_prefix() + '''
extract_task_key_from_branch "feature/abc-123-do-stuff" || true
extract_task_key_from_branch "ABC-456" || true
extract_task_key_from_branch "bugfix/lower-xyz-789" || true
extract_task_key_from_branch "no-ticket-here" || echo NONE
''')
        lines = [l for l in out.strip().splitlines() if l]
        self.assertEqual(lines, ["ABC-123", "ABC-456", "XYZ-789", "NONE"])

    def test_normalize_and_validate_task(self):
        rc, out, err = self._run(self._source_prefix() + '''
normalize_and_validate_task "  abc-123  " || true
normalize_and_validate_task "ABC-123" || true
normalize_and_validate_task "unassigned" || echo INVALID
normalize_and_validate_task "bad-key" || echo INVALID
''')
        lines = [l for l in out.strip().splitlines() if l]
        # The bash normalizer validates the branch-derived key shape
        # only; the UNASSIGNED sentinel is handled by resolve_task_id, not
        # here.
        self.assertEqual(lines, ["ABC-123", "ABC-123", "INVALID", "INVALID"])

    def test_normalize_and_validate_task_delegates_to_real_python_helper(self):
        # Finding 6 regression: non-KEY-123-style input must actually be
        # delegated to the REAL `copilot-task-report.py normalize-task-id`
        # (not a fake/stubbed helper), exercising the genuine bash->python
        # subprocess handoff end to end: generic free-form names, the
        # UNASSIGNED sentinel, and rejection of unsafe input (path
        # traversal, a leading dash, and an all-dots name).
        real_helper = os.path.join(os.path.dirname(self.SCRIPT), "copilot-task-report.py")
        self.assertTrue(os.path.isfile(real_helper), "real copilot-task-report.py helper must exist")
        rc, out, err = self._run(
            self._source_prefix() + '''
normalize_and_validate_task "My Cool Task" || true
normalize_and_validate_task "UNASSIGNED" || true
normalize_and_validate_task "unassigned" || true
normalize_and_validate_task "../../etc/passwd" || echo INVALID
normalize_and_validate_task "-leading-dash" || echo INVALID
normalize_and_validate_task "..." || echo INVALID
''',
            env={"COPILOT_TASK_REPORT_HELPER": real_helper},
        )
        lines = [l for l in out.strip().splitlines() if l]
        self.assertEqual(
            lines,
            ["my-cool-task", "UNASSIGNED", "UNASSIGNED", "INVALID", "INVALID", "INVALID"],
            err,
        )

    def test_resolve_task_id_from_current_branch(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature/PROJ-123-desc"], cwd=repo, check=True)
        rc, out, err = self._run(self._source_prefix() + 'resolve_task_id ""', cwd=repo)
        self.assertEqual(out.strip(), "PROJ-123")

    def test_resolve_task_id_from_stored_branch(self):
        session_dir = os.path.join(self.home, ".copilot", "session-state", "sess-store")
        os.makedirs(session_dir)
        with open(os.path.join(session_dir, "workspace.yaml"), "w") as f:
            f.write("branch: feature/store-456-stuff\n")
        rc, out, err = self._run(self._source_prefix() + 'resolve_task_id "sess-store"')
        self.assertEqual(out.strip(), "STORE-456")

    def test_resolve_task_id_unassigned_on_empty_input(self):
        rc, out, err = self._run(self._source_prefix() + 'resolve_task_id ""', stdin_text="\n")
        self.assertEqual(out.strip(), "UNASSIGNED")
        self.assertIn("No task ID detected", err)

    def test_resolve_task_id_invalid_three_times_defaults_unassigned(self):
        rc, out, err = self._run(
            self._source_prefix() + 'resolve_task_id ""',
            stdin_text="bad\nalso-bad\nstill-bad\n",
        )
        self.assertEqual(out.strip(), "UNASSIGNED")
        self.assertIn("Too many invalid attempts", err)

    def test_resolve_task_id_valid_input_normalized(self):
        rc, out, err = self._run(
            self._source_prefix() + 'resolve_task_id ""',
            stdin_text="  myticket-42  \n",
        )
        self.assertEqual(out.strip(), "MYTICKET-42")

    def test_update_task_report_is_nonfatal_on_helper_failure(self):
        # The helper must see a present session dir so it can proceed to
        # the forced-failure path and prove update_task_report is nonfatal.
        self._make_session_dir("sess-fail")
        rc, out, err = self._run(
            self._source_prefix() + 'update_task_report "sess-fail" "TEST-1"',
            env={"COPILOT_TASK_REPORT_FAIL": "1"},
        )
        self.assertEqual(rc, 0)
        self.assertIn("task usage report update failed", err)
        self.assertIn("helper forced failure", err)

    def test_copilot_s_report_flag(self):
        rc, out, err = self._run("'%s' --report ABC-123" % self.SCRIPT)
        self.assertEqual(rc, 0)
        self.assertIn("report for ABC-123", out)

    def test_copilot_s_report_equals_form(self):
        rc, out, err = self._run("'%s' --report=DEF-456" % self.SCRIPT)
        self.assertEqual(rc, 0)
        self.assertIn("report for DEF-456", out)

    def test_copilot_s_report_flag_at_end(self):
        rc, out, err = self._run("'%s' --all --report GHI-789" % self.SCRIPT)
        self.assertEqual(rc, 0)
        self.assertIn("report for GHI-789", out)

    def test_copilot_s_report_invalid_key_fails(self):
        rc, out, err = self._run("'%s' --report invalid-key" % self.SCRIPT)
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid key", err)

    def _make_session_dir(self, sid, branch="feature/TEST-1-stuff", with_events=False):
        d = os.path.join(self.home, ".copilot", "session-state", sid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "workspace.yaml"), "w") as f:
            f.write(f"created_at: 2026-01-01T00:00:00\ncwd: /tmp/repo\nbranch: {branch}\n")
        if with_events:
            with open(os.path.join(d, "events.jsonl"), "w") as f:
                f.write('{"type":"session.start","timestamp":"2026-01-01T00:00:00Z","data":{}}\n')
        return d

    def _helper_log(self):
        log_path = os.path.join(self.home, "helper.log")
        if not os.path.exists(log_path):
            return ""
        with open(log_path) as f:
            return f.read()

    def test_post_session_keep_runs_ingest_before_exit(self):
        sid = "sess-keep"
        self._make_session_dir(sid, branch="feature/KEEP-1-stuff")
        rc, out, err = self._run(
            self._source_prefix() + 'post_session "%s" "KEEP-1"' % sid,
            stdin_text="k\n",
        )
        self.assertEqual(rc, 0)
        self.assertIn("kept", out.lower())
        log = self._helper_log()
        self.assertIn("ingest session=%s task=KEEP-1 dir_exists=True" % sid, log)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".copilot", "session-state", sid)))

    def test_post_session_rename_runs_ingest_before_exit(self):
        sid = "sess-rename"
        self._make_session_dir(sid, branch="feature/RENAME-1-stuff")
        rc, out, err = self._run(
            self._source_prefix() + 'post_session "%s" "RENAME-1"' % sid,
            stdin_text="r\nmy-new-name\n",
        )
        self.assertEqual(rc, 0)
        self.assertIn("saved", out.lower())
        log = self._helper_log()
        self.assertIn("ingest session=%s task=RENAME-1 dir_exists=True" % sid, log)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".copilot", "session-state", sid)))

    def test_post_session_delete_runs_ingest_before_rm_rf(self):
        sid = "sess-delete"
        self._make_session_dir(sid, branch="feature/DELETE-1-stuff", with_events=True)
        rc, out, err = self._run(
            self._source_prefix() + 'post_session "%s" "DELETE-1"' % sid,
            stdin_text="d\n",
        )
        self.assertEqual(rc, 0)
        self.assertIn("deleted", out.lower())
        log = self._helper_log()
        self.assertIn("ingest session=%s task=DELETE-1 dir_exists=True events_exists=True" % sid, log)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", sid)))

    def test_multi_delete_ingests_before_deletion(self):
        # Set up two sessions with stored branches so noninteractive
        # resolution finds a task ID.
        for sid, num in (("del-a", "1"), ("del-b", "2")):
            d = os.path.join(self.home, ".copilot", "session-state", sid)
            os.makedirs(d)
            with open(os.path.join(d, "workspace.yaml"), "w") as f:
                f.write(f"branch: feature/PROJ-{num}-stuff\n")
        sessions_file = os.path.join(self.home, ".copilot-sessions")
        filtered_file = os.path.join(self.tmp, "filtered.txt")
        with open(sessions_file, "w") as f:
            f.write("del-a|name a|2026-01-01T00:00:00|/cwd\n")
            f.write("del-b|name b|2026-01-01T00:00:00|/cwd\n")
        with open(filtered_file, "w") as f:
            f.write("del-a|name a|2026-01-01T00:00:00|/cwd\n")
            f.write("del-b|name b|2026-01-01T00:00:00|/cwd\n")
        snippet = self._source_prefix() + '''
FILTERED_FILE="%s"
SESSIONS_FILE="%s"
delete_session
''' % (filtered_file, sessions_file)
        rc, out, err = self._run(snippet, stdin_text="1 2\ny\n")
        self.assertEqual(rc, 0)
        self.assertIn("Deleted 2 session(s)", out)
        # Both sessions should have been ingested before deletion.
        log_path = os.path.join(self.home, "helper.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            log = f.read()
        self.assertIn("ingest session=del-a task=PROJ-1 dir_exists=True events_exists=False", log)
        self.assertIn("ingest session=del-b task=PROJ-2 dir_exists=True events_exists=False", log)
        # Session directories must be gone.
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", "del-a")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", "del-b")))
        # Sessions file rewritten without deleted IDs.
        with open(sessions_file) as f:
            remaining = f.read()
        self.assertNotIn("del-a", remaining)
        self.assertNotIn("del-b", remaining)


class TestNormalizeTaskIdEdgeCases(unittest.TestCase):
    """Direct unit coverage of normalize_task_id()'s two-tier scheme:
    conservative KEY-123-style IDs are uppercased as-is; everything else
    is treated as a generic free-form name and sanitized safely."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def norm(self, raw):
        return self.mod.normalize_task_id(raw)

    def test_key_style_id_is_uppercased(self):
        self.assertEqual(self.norm("abc-123"), "ABC-123")
        self.assertEqual(self.norm("ABC-123"), "ABC-123")
        self.assertEqual(self.norm("  abc-123  "), "ABC-123")

    def test_unassigned_passes_through_case_insensitively(self):
        self.assertEqual(self.norm("unassigned"), self.mod.UNASSIGNED)
        self.assertEqual(self.norm("UNASSIGNED"), self.mod.UNASSIGNED)
        self.assertEqual(self.norm("  Unassigned  "), self.mod.UNASSIGNED)

    def test_generic_name_is_lowercased_and_whitespace_collapsed(self):
        self.assertEqual(self.norm("My Cool   Task"), "my-cool-task")
        self.assertEqual(self.norm("  spaced out  "), "spaced-out")

    def test_generic_name_allows_dots_underscores_hyphens(self):
        self.assertEqual(self.norm("release_2026.09_v2"), "release_2026.09_v2")

    def test_rejects_path_traversal(self):
        self.assertIsNone(self.norm("../../etc/passwd"))
        self.assertIsNone(self.norm("../foo"))
        self.assertIsNone(self.norm("a/../b"))
        self.assertIsNone(self.norm("a\\b"))

    def test_rejects_control_characters(self):
        self.assertIsNone(self.norm("bad\x00name"))
        self.assertIsNone(self.norm("bad\nname"))
        self.assertIsNone(self.norm("bad\x7fname"))

    def test_rejects_unsafe_punctuation(self):
        self.assertIsNone(self.norm("not a valid key!!"))
        self.assertIsNone(self.norm("weird$(injection)"))

    def test_rejects_over_max_length(self):
        self.assertIsNone(self.norm("a" * (self.mod.MAX_TASK_ID_LEN + 1)))

    def test_accepts_at_max_length(self):
        s = "a" * self.mod.MAX_TASK_ID_LEN
        self.assertEqual(self.norm(s), s)

    def test_rejects_empty_and_none(self):
        self.assertIsNone(self.norm(""))
        self.assertIsNone(self.norm("   "))
        self.assertIsNone(self.norm(None))

    def test_case_folding_prevents_collisions(self):
        # Two names differing only by case must normalize identically so
        # they can never collide on a case-insensitive filesystem.
        self.assertEqual(self.norm("My Task"), self.norm("MY TASK"))
        self.assertEqual(self.norm("My Task"), self.norm("my task"))

    def test_rejects_leading_dash_generic_name(self):
        # A leading '-' would make the normalized id look like a CLI flag
        # to any call site not scrupulously careful with `--`/`=` forms;
        # reject it outright as an independent defense layer.
        self.assertIsNone(self.norm("-rf"))
        self.assertIsNone(self.norm("--help"))
        self.assertIsNone(self.norm("-my-task"))

    def test_rejects_over_max_length_even_when_key_style(self):
        # The length cap must apply BEFORE the KEY-123 fast-path match,
        # not just on the generic branch: an absurdly long (but otherwise
        # KEY-123-shaped) string must still be rejected rather than
        # returned as-is, bypassing MAX_TASK_ID_LEN entirely.
        overlong_key = "A" * (self.mod.MAX_TASK_ID_LEN + 5) + "-123"
        self.assertIsNone(self.norm(overlong_key))

    def test_accepts_key_style_at_exactly_max_length(self):
        key = "A" * (self.mod.MAX_TASK_ID_LEN - 4) + "-123"
        self.assertEqual(len(key), self.mod.MAX_TASK_ID_LEN)
        self.assertEqual(self.norm(key), key)

    def test_rejects_dots_only_ids(self):
        # A name consisting ENTIRELY of dots ('.', '..', '...', etc.) is a
        # filesystem self/parent-dir reference and must never be accepted
        # as a task id, even though a lone '.' (or three-or-more dots)
        # doesn't contain the literal '..' substring the traversal check
        # looks for.
        self.assertIsNone(self.norm("."))
        self.assertIsNone(self.norm(".."))
        self.assertIsNone(self.norm("..."))
        self.assertIsNone(self.norm("...."))
        self.assertIsNone(self.norm("  .  "))
        self.assertIsNone(self.norm("  ...  "))

    def test_dot_containing_but_not_dots_only_id_still_accepted(self):
        # The dots-only rejection must not become an overly broad "any
        # dot is unsafe" rule: a name that legitimately contains dots
        # alongside other safe characters is still valid.
        self.assertEqual(self.norm("release.2026.09"), "release.2026.09")


class TestMigrateLegacyTaskStorage(BaseTestCase):
    """migrate_legacy_task_storage(): idempotent, non-destructive migration
    of ~/.copilot/jira-reports (pre-2.0) into ~/.copilot/task-reports."""

    def setUp(self):
        super().setUp()
        self.legacy_dir = os.path.join(self.tmp, "jira-reports")
        self.mod.LEGACY_SUPPORT_DIR = self.legacy_dir
        os.makedirs(self.legacy_dir, exist_ok=True)

    def _write_legacy_pricing(self, value="legacy-edited"):
        with open(os.path.join(self.legacy_dir, "model-pricing.json"), "w", encoding="utf-8") as f:
            json.dump({"updated_at": value, "models": {}, "aliases": {}}, f)

    def _write_legacy_marker(self):
        with open(os.path.join(self.legacy_dir, "install-marker.json"), "w", encoding="utf-8") as f:
            json.dump({"installed_at": "2025-01-01T00:00:00Z", "installed_epoch": 1735689600.0}, f)

    def _write_legacy_ticket(self, fname, task_id=None, jira_key=None, extra=None):
        tdir = os.path.join(self.legacy_dir, "tickets")
        os.makedirs(tdir, exist_ok=True)
        data = {"totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}
        if task_id is not None:
            data["task_id"] = task_id
        if jira_key is not None:
            data["jira_key"] = jira_key
        if extra:
            data.update(extra)
        with open(os.path.join(tdir, fname), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_noop_when_legacy_dir_absent(self):
        shutil.rmtree(self.legacy_dir)
        # Must not raise or create anything.
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_pricing_copied_only_if_absent_preserving_user_edits(self):
        self._write_legacy_pricing("legacy-edited-value")
        # New-side pricing already exists (e.g. shipped default) — must be
        # left completely untouched, never overwritten by the legacy copy.
        self.mod.atomic_write(
            self.mod.PRICING_FILE,
            json.dumps({"updated_at": "already-here", "models": {}, "aliases": {}}),
        )
        self.mod.migrate_legacy_task_storage()
        with open(self.mod.PRICING_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["updated_at"], "already-here")

    def test_pricing_migrated_when_new_side_absent(self):
        os.remove(self.mod.PRICING_FILE)  # BaseTestCase seeds a fixture; remove it for this "absent" case
        self._write_legacy_pricing("legacy-edited-value")
        self.assertFalse(os.path.exists(self.mod.PRICING_FILE))
        self.mod.migrate_legacy_task_storage()
        with open(self.mod.PRICING_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["updated_at"], "legacy-edited-value")

    def test_marker_migrated_preserving_original_install_epoch(self):
        os.remove(self.mod.INSTALL_MARKER_FILE)  # BaseTestCase seeds a fixture; remove it for this "absent" case
        self._write_legacy_marker()
        self.mod.migrate_legacy_task_storage()
        with open(self.mod.INSTALL_MARKER_FILE, encoding="utf-8") as f:
            marker = json.load(f)
        self.assertEqual(marker["installed_epoch"], 1735689600.0)

    def test_jira_key_json_field_converted_to_task_id_on_write(self):
        self._write_legacy_ticket("ABC-123.json", jira_key="ABC-123")
        self.mod.migrate_legacy_task_storage()
        migrated = self.mod.load_json(self.mod.task_path("ABC-123"), None)
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated["task_id"], "ABC-123")
        self.assertNotIn("jira_key", migrated)

    def test_session_state_jira_key_converted_and_merged(self):
        legacy_state = {
            "sess-legacy": {"jira_key": "ABC-123"},
        }
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_state, f)
        self.mod.migrate_legacy_task_storage()
        new_state = self.mod.load_json(self.mod.STATE_FILE, {})
        self.assertEqual(new_state["sess-legacy"]["task_id"], "ABC-123")
        self.assertNotIn("jira_key", new_state["sess-legacy"])

    def test_session_state_does_not_overwrite_existing_new_side_entry(self):
        legacy_state = {"sess-a": {"jira_key": "OLD-1"}}
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_state, f)
        self.mod.atomic_write(self.mod.STATE_FILE, json.dumps({"sess-a": {"task_id": "NEW-1"}}))
        self.mod.migrate_legacy_task_storage()
        new_state = self.mod.load_json(self.mod.STATE_FILE, {})
        self.assertEqual(new_state["sess-a"]["task_id"], "NEW-1", "must not overwrite an already-tracked session")

    def test_legacy_dir_removed_after_full_success(self):
        self._write_legacy_pricing()
        self._write_legacy_marker()
        self._write_legacy_ticket("ABC-1.json", jira_key="ABC-1")
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_conflicting_ticket_preserves_both_and_keeps_legacy_dir(self):
        self._write_legacy_ticket("ABC-1.json", jira_key="ABC-1", extra={"custom_marker": "legacy-value"})
        # New side already has a *different* task file under the same id.
        self.mod.atomic_write(
            self.mod.task_path("ABC-1"),
            json.dumps({"task_id": "ABC-1", "custom_marker": "new-value",
                        "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
        )
        self.mod.migrate_legacy_task_storage()
        # New-side file must be untouched (not overwritten by legacy data).
        new_side = self.mod.load_json(self.mod.task_path("ABC-1"), None)
        self.assertEqual(new_side["custom_marker"], "new-value")
        # Legacy directory must be left in place (unresolved conflict) so
        # the next run can retry / a human can inspect it.
        self.assertTrue(os.path.exists(self.legacy_dir))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "tickets", "ABC-1.json")))

    def test_identical_ticket_content_treated_as_already_migrated(self):
        self._write_legacy_ticket("ABC-1.json", task_id="ABC-1")
        legacy_data = self.mod.load_json(os.path.join(self.legacy_dir, "tickets", "ABC-1.json"), {})
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(legacy_data))
        self.mod.migrate_legacy_task_storage()
        # No conflict (identical content) -> legacy dir fully cleaned up.
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_idempotent_second_run_is_a_noop(self):
        self._write_legacy_pricing("legacy-value")
        self._write_legacy_marker()
        self._write_legacy_ticket("ABC-1.json", jira_key="ABC-1")
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))
        snapshot_pricing = self.mod.load_json(self.mod.PRICING_FILE, {})
        snapshot_task = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        # Running again with the legacy dir already gone must be a no-op.
        self.mod.migrate_legacy_task_storage()
        self.assertEqual(self.mod.load_json(self.mod.PRICING_FILE, {}), snapshot_pricing)
        self.assertEqual(self.mod.load_json(self.mod.task_path("ABC-1"), {}), snapshot_task)

    def test_legacy_id_normalized_via_generic_scheme_when_not_key_style(self):
        # A legacy free-form name (not KEY-123-style) must still land at
        # the normalized destination path through the same generic scheme
        # used everywhere else (case-folded, whitespace collapsed).
        self._write_legacy_ticket("My Cool Task.json", jira_key="My Cool Task")
        self.mod.migrate_legacy_task_storage()
        migrated = self.mod.load_json(self.mod.task_path("my-cool-task"), None)
        self.assertIsNotNone(migrated)
        self.assertNotIn("jira_key", migrated)

    def test_unknown_top_level_file_preserved_verbatim_then_source_removed(self):
        # An unrecognized top-level entry (e.g. a user's own notes file,
        # or something from a future/unknown version) must never be
        # silently dropped by the final rmtree — it must be verifiably
        # copied to legacy-unmigrated/ first. Once that copy succeeds,
        # it's safe to finish cleaning up the legacy dir in the same run.
        with open(os.path.join(self.legacy_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("some private note the user left in the legacy dir")
        self.mod.migrate_legacy_task_storage()
        preserved = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "notes.txt")
        self.assertTrue(os.path.exists(preserved))
        with open(preserved, encoding="utf-8") as f:
            self.assertEqual(f.read(), "some private note the user left in the legacy dir")
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_unknown_top_level_subdir_recursively_preserved_then_source_removed(self):
        nested = os.path.join(self.legacy_dir, "custom-extra", "nested")
        os.makedirs(nested)
        with open(os.path.join(nested, "deep.json"), "w", encoding="utf-8") as f:
            f.write('{"anything": true}')
        self.mod.migrate_legacy_task_storage()
        preserved = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "custom-extra", "nested", "deep.json")
        self.assertTrue(os.path.exists(preserved))
        with open(preserved, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"anything": true}')
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_unknown_top_level_subdir_partial_previous_copy_blocks_removal(self):
        # Fixture simulating a previous run that crashed/was interrupted
        # partway through copying an unknown subdirectory: the
        # legacy-unmigrated/ destination exists but is missing one of the
        # two files the (still-intact) legacy source has. This must
        # never be mistaken for "already preserved" just because the
        # destination directory exists — it must block removal of the
        # legacy source, and must never overwrite/merge into the partial
        # copy silently.
        nested = os.path.join(self.legacy_dir, "custom-extra")
        os.makedirs(nested)
        with open(os.path.join(nested, "a.json"), "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        with open(os.path.join(nested, "b.json"), "w", encoding="utf-8") as f:
            f.write('{"b": 2}')
        partial_dst = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "custom-extra")
        os.makedirs(partial_dst)
        with open(os.path.join(partial_dst, "a.json"), "w", encoding="utf-8") as f:
            f.write('{"a": 1}')  # only "a.json" made it before the simulated crash

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()

        self.assertTrue(os.path.exists(self.legacy_dir), "partial previous copy must block source removal")
        self.assertTrue(os.path.exists(os.path.join(nested, "a.json")))
        self.assertTrue(os.path.exists(os.path.join(nested, "b.json")))
        # The partial destination must be left exactly as it was found —
        # never silently completed/merged, never overwritten.
        self.assertFalse(os.path.exists(os.path.join(partial_dst, "b.json")))
        with open(os.path.join(partial_dst, "a.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"a": 1}')
        self.assertIn("unresolved", stderr_capture.getvalue())

        # Once the partial copy is completed (e.g. manually, or by
        # removing it so a fresh verified copy can be made), the source
        # is cleaned up normally.
        with open(os.path.join(partial_dst, "b.json"), "w", encoding="utf-8") as f:
            f.write('{"b": 2}')
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_unknown_top_level_entry_conflict_blocks_removal_and_preserves_source(self):
        # If legacy-unmigrated/ already has a DIFFERENT file at that path
        # (e.g. a previous partial/aborted run left something there under
        # unrelated circumstances), the unknown entry must never be
        # silently overwritten, and the legacy source must be preserved.
        os.makedirs(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated"), exist_ok=True)
        with open(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "notes.txt"), "w", encoding="utf-8") as f:
            f.write("already-there different content")
        with open(os.path.join(self.legacy_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("legacy content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        self.assertTrue(os.path.exists(self.legacy_dir))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "notes.txt")))
        self.assertIn("unresolved", stderr_capture.getvalue())


    def test_blocked_migration_recovers_and_finishes_on_next_run_once_resolved(self):
        # Simulate a run that's blocked by a conflicting legacy-unmigrated/
        # entry; once the conflicting entry is manually resolved (as the
        # visible warning instructs the user to do), a subsequent run
        # must finish the cleanup rather than requiring re-migration.
        os.makedirs(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated"), exist_ok=True)
        conflict_path = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "notes.txt")
        with open(conflict_path, "w", encoding="utf-8") as f:
            f.write("different pre-existing content")
        with open(os.path.join(self.legacy_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("legacy content")
        self.mod.migrate_legacy_task_storage()
        self.assertTrue(os.path.exists(self.legacy_dir))
        # User resolves the conflict by removing the stale blocker.
        os.remove(conflict_path)
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))
        with open(conflict_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "legacy content")

    def test_non_json_entry_in_tickets_dir_preserved_verbatim(self):
        tdir = os.path.join(self.legacy_dir, "tickets")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "README.txt"), "w", encoding="utf-8") as f:
            f.write("some readme content that isn't a ticket")
        os.makedirs(os.path.join(tdir, "attachments"))
        with open(os.path.join(tdir, "attachments", "file.bin"), "wb") as f:
            f.write(b"\x00\x01binary-data")
        self.mod.migrate_legacy_task_storage()
        base = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "tickets")
        with open(os.path.join(base, "README.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "some readme content that isn't a ticket")
        with open(os.path.join(base, "attachments", "file.bin"), "rb") as f:
            self.assertEqual(f.read(), b"\x00\x01binary-data")
        # Everything was verifiably preserved with no conflicts -> safe
        # to finish cleaning up the legacy source in this same run.
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_symlink_in_legacy_dir_is_never_migrated_or_silently_dropped(self):
        target = os.path.join(self.tmp, "outside-target.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("outside content")
        link = os.path.join(self.legacy_dir, "sneaky-link")
        os.symlink(target, link)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        preserved = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "sneaky-link")
        self.assertFalse(os.path.exists(preserved), "a symlink must never be migrated/followed")
        self.assertTrue(os.path.exists(self.legacy_dir), "unresolved symlink must block legacy dir removal")
        self.assertIn("symlink", stderr_capture.getvalue())

    def test_corrupt_json_session_state_blocks_migration_and_preserves_source(self):
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        # Must never be silently swallowed/removed: legacy dir and the
        # corrupt file itself must both still be present, and the new
        # state file must not have been created/corrupted from garbage.
        self.assertTrue(os.path.exists(self.legacy_dir))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "session-state.json")))
        self.assertIn("unresolved", stderr_capture.getvalue())

    def test_non_dict_session_state_blocks_migration_and_preserves_source(self):
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        self.assertTrue(os.path.exists(self.legacy_dir))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "session-state.json")))
        self.assertIn("unresolved", stderr_capture.getvalue())
        # The new-side state file must be untouched/absent, never
        # populated from garbage input.
        self.assertEqual(self.mod.load_json(self.mod.STATE_FILE, {}), {})

    def test_non_dict_session_state_entry_reported_and_skipped_not_crashed(self):
        legacy_state = {
            "sess-good": {"jira_key": "ABC-1"},
            "sess-bad": "not-a-dict-entry",
        }
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_state, f)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        new_state = self.mod.load_json(self.mod.STATE_FILE, {})
        self.assertEqual(new_state["sess-good"]["task_id"], "ABC-1")
        self.assertNotIn("sess-bad", new_state)
        self.assertIn("sess-bad", stderr_capture.getvalue())

    def test_migrated_json_task_id_field_always_matches_canonical_destination(self):
        # Even if the legacy JSON body already had a raw/un-normalized
        # "task_id" value (extra whitespace, mixed case), the written
        # field must be forced to the exact canonical id used for the
        # destination path — never left as the stale raw value.
        self._write_legacy_ticket("weird-name.json", task_id="  My Task  ")
        self.mod.migrate_legacy_task_storage()
        migrated = self.mod.load_json(self.mod.task_path("my-task"), None)
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated["task_id"], "my-task")


    def test_session_state_task_id_normalized_through_same_canonical_scheme(self):
        legacy_state = {"sess-x": {"jira_key": "My Free Task"}}
        with open(os.path.join(self.legacy_dir, "session-state.json"), "w", encoding="utf-8") as f:
            json.dump(legacy_state, f)
        self.mod.migrate_legacy_task_storage()
        new_state = self.mod.load_json(self.mod.STATE_FILE, {})
        self.assertEqual(new_state["sess-x"]["task_id"], "my-free-task")

    # --- Finding 4: differing legacy install-marker.json/model-pricing.json
    # must be preserved, never silently deleted, when a different file
    # already exists at the new location. ---

    def test_differing_legacy_pricing_preserved_under_legacy_unmigrated(self):
        self._write_legacy_pricing("legacy-edited-value")
        self.mod.atomic_write(
            self.mod.PRICING_FILE,
            json.dumps({"updated_at": "already-here", "models": {}, "aliases": {}}),
        )
        self.mod.migrate_legacy_task_storage()
        # New-side pricing untouched.
        with open(self.mod.PRICING_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["updated_at"], "already-here")
        # Differing legacy pricing preserved verbatim, not silently dropped.
        preserved = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "model-pricing.json")
        self.assertTrue(os.path.exists(preserved))
        with open(preserved, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["updated_at"], "legacy-edited-value")
        # Nothing blocks the (now fully accounted-for) legacy dir removal.
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_differing_legacy_install_marker_preserved_under_legacy_unmigrated(self):
        self.mod.atomic_write(
            self.mod.INSTALL_MARKER_FILE,
            json.dumps({"installed_at": "2026-01-01T00:00:00Z", "installed_epoch": 1767225600.0}),
        )
        self._write_legacy_marker()  # different installed_epoch (2025-01-01)
        self.mod.migrate_legacy_task_storage()
        with open(self.mod.INSTALL_MARKER_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["installed_epoch"], 1767225600.0)
        preserved = os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "install-marker.json")
        self.assertTrue(os.path.exists(preserved))
        with open(preserved, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["installed_epoch"], 1735689600.0)
        self.assertFalse(os.path.exists(self.legacy_dir))

    def test_differing_legacy_pricing_conflicting_with_prior_unmigrated_copy_blocks_cleanup(self):
        # If legacy-unmigrated/model-pricing.json already holds yet a
        # THIRD, different snapshot (e.g. from an earlier partial run),
        # the new differing legacy copy must never silently overwrite it
        # -- it must block cleanup with a clear warning instead.
        os.makedirs(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated"), exist_ok=True)
        with open(
            os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "model-pricing.json"), "w", encoding="utf-8"
        ) as f:
            f.write('{"updated_at": "stale-preserved-copy"}')
        self._write_legacy_pricing("legacy-edited-value")
        self.mod.atomic_write(
            self.mod.PRICING_FILE,
            json.dumps({"updated_at": "already-here", "models": {}, "aliases": {}}),
        )
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        self.assertTrue(os.path.exists(self.legacy_dir), "unresolved conflict must block legacy dir removal")
        self.assertTrue(os.path.exists(os.path.join(self.legacy_dir, "model-pricing.json")))
        self.assertIn("unresolved", stderr_capture.getvalue())
        # Never overwrote the earlier-preserved snapshot.
        with open(
            os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "model-pricing.json"), encoding="utf-8"
        ) as f:
            self.assertEqual(json.load(f)["updated_at"], "stale-preserved-copy")

    # --- Finding 3: a legacy tickets/*.json entry that was already
    # migrated by a previous (possibly blocked) run must never be
    # re-flagged as conflicting just because the live new-side task file
    # has since legitimately diverged (e.g. via ingest of newer usage). ---

    def test_blocked_migration_then_live_ingest_then_unblock_finishes_and_keeps_newer_totals(self):
        # 1) One ticket ("ABC-1") migrates cleanly on the first run, but
        #    an unrelated top-level conflict blocks legacy dir removal.
        self._write_legacy_ticket("ABC-1.json", task_id="ABC-1")
        os.makedirs(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated"), exist_ok=True)
        with open(
            os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "blocker.txt"), "w", encoding="utf-8"
        ) as f:
            f.write("pre-existing different content")
        with open(os.path.join(self.legacy_dir, "blocker.txt"), "w", encoding="utf-8") as f:
            f.write("legacy content")

        self.mod.migrate_legacy_task_storage()
        self.assertTrue(os.path.exists(self.legacy_dir), "unrelated conflict must keep legacy dir around")
        migrated_after_first_run = self.mod.load_json(self.mod.task_path("ABC-1"), None)
        self.assertIsNotNone(migrated_after_first_run)

        # 2) Live usage updates the now-migrated ABC-1 task file with
        #    NEWER totals (simulating a real ingest happening while the
        #    legacy dir is still present, blocked on the unrelated item).
        newer = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        newer["totals"]["calls"] = 42
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(newer))

        # 3) A migration re-run at this point must NOT treat the now-
        #    diverged live file as a fresh conflict against the stale
        #    legacy snapshot (it was already migrated once).
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        self.assertNotIn("ABC-1", stderr_capture.getvalue())
        current = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        self.assertEqual(current["totals"]["calls"], 42, "newer live totals must never be reverted")
        self.assertTrue(os.path.exists(self.legacy_dir), "still blocked by the unrelated top-level conflict")

        # 4) User resolves the unrelated conflict; the next run must
        #    finally finish and remove the legacy dir, while STILL never
        #    touching the now-live, newer ABC-1 task data.
        os.remove(os.path.join(self.mod.SUPPORT_DIR, "legacy-unmigrated", "blocker.txt"))
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))
        final = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        self.assertEqual(final["totals"]["calls"], 42, "newer live totals must survive full migration completion")

    def test_restored_legacy_dir_after_full_removal_is_treated_fresh_not_silently_dropped(self):
        # 1) Migrate one ticket cleanly; the legacy dir (and its
        #    provenance marker) must be fully removed on success.
        self._write_legacy_ticket("ABC-1.json", task_id="ABC-1")
        self.mod.migrate_legacy_task_storage()
        self.assertFalse(os.path.exists(self.legacy_dir))
        marker_path = os.path.join(self.mod.SUPPORT_DIR, self.mod.LEGACY_TICKET_MIGRATION_MARKER_NAME)
        self.assertFalse(
            os.path.exists(marker_path),
            "the provenance marker must be pruned the moment its legacy source is fully removed",
        )

        # 2) Live usage diverges the migrated task file afterward (e.g.
        #    real ingest activity).
        live = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        live["totals"]["calls"] = 7
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(live))

        # 3) The legacy directory is later RESTORED (e.g. from a backup)
        #    with the SAME original snapshot content that was migrated in
        #    step 1. Because the marker no longer exists, this must be
        #    treated as a brand-new occurrence: a genuine conflict against
        #    the now-diverged live file, not a silently-skipped no-op.
        os.makedirs(self.legacy_dir, exist_ok=True)
        self._write_legacy_ticket("ABC-1.json", task_id="ABC-1")

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_task_storage()
        self.assertIn(
            "ABC-1",
            stderr_capture.getvalue(),
            "a restored legacy source must be freshly compared/flagged, never silently dropped "
            "via a stale marker fingerprint",
        )
        self.assertTrue(
            os.path.exists(self.legacy_dir),
            "an unresolved fresh conflict must keep the restored legacy dir in place",
        )
        current = self.mod.load_json(self.mod.task_path("ABC-1"), {})
        self.assertEqual(current["totals"]["calls"], 7, "live data must remain untouched by the restored legacy copy")


class TestMigrateLegacyDesktopReports(BaseTestCase):
    """migrate_legacy_desktop_reports(): merge-without-overwrite migration
    of ~/Desktop/CopilotJiraTaskReports (pre-2.0) into the current Desktop
    reports directory."""

    def setUp(self):
        super().setUp()
        self.legacy_desktop = os.path.join(self.tmp, "Desktop", "CopilotJiraTaskReports")
        self.mod.LEGACY_DESKTOP_DIR = self.legacy_desktop
        os.makedirs(self.legacy_desktop, exist_ok=True)

    def _write_legacy_file(self, name, content):
        os.makedirs(self.legacy_desktop, exist_ok=True)
        with open(os.path.join(self.legacy_desktop, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_noop_when_legacy_dir_absent(self):
        shutil.rmtree(self.legacy_desktop)
        self.mod.migrate_legacy_desktop_reports()
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_noop_when_legacy_and_new_dirs_are_the_same_path(self):
        self.mod.REPORTS_DIR = self.legacy_desktop
        self._write_legacy_file("ABC-1.md", "content")
        self.mod.migrate_legacy_desktop_reports()
        # Must not delete the directory it's also using as the destination.
        self.assertTrue(os.path.exists(self.legacy_desktop))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_desktop, "ABC-1.md")))

    def test_new_only_file_is_copied_over(self):
        self._write_legacy_file("ABC-1.md", "legacy report content")
        self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")
        self.assertTrue(os.path.exists(dst))
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "legacy report content")
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_identical_file_is_deduped_not_duplicated(self):
        os.makedirs(self.mod.REPORTS_DIR, exist_ok=True)
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), "w", encoding="utf-8") as f:
            f.write("same content")
        self._write_legacy_file("ABC-1.md", "same content")
        self.mod.migrate_legacy_desktop_reports()
        entries = os.listdir(self.mod.REPORTS_DIR)
        self.assertEqual(entries, ["ABC-1.md"], "identical legacy file must be deduped, not duplicated")
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_conflicting_file_preserves_both_with_legacy_conflict_suffix(self):
        os.makedirs(self.mod.REPORTS_DIR, exist_ok=True)
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), "w", encoding="utf-8") as f:
            f.write("new content")
        self._write_legacy_file("ABC-1.md", "different legacy content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        # Existing (new) file must be untouched.
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new content")
        # Legacy content preserved under the conflict-suffixed name.
        conflict_path = os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md")
        self.assertTrue(os.path.exists(conflict_path))
        with open(conflict_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "different legacy content")
        self.assertIn("conflicting", stderr_capture.getvalue())
        # Never overwrites, never loses data -> safe to remove the legacy dir.
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_idempotent_second_run_is_a_noop(self):
        self._write_legacy_file("ABC-1.md", "content")
        self.mod.migrate_legacy_desktop_reports()
        self.assertFalse(os.path.exists(self.legacy_desktop))
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), encoding="utf-8") as f:
            snapshot = f.read()
        # Running again with the legacy dir already gone must be a no-op.
        self.mod.migrate_legacy_desktop_reports()
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), snapshot)

    def test_unknown_nested_subdir_is_recursively_preserved(self):
        # Any nested subdirectory/unknown file layout (not just top-level
        # .md reports) must be walked recursively and preserved, never
        # silently dropped by the final rmtree.
        nested = os.path.join(self.legacy_desktop, "archive", "2023")
        os.makedirs(nested)
        with open(os.path.join(nested, "old-notes.txt"), "w", encoding="utf-8") as f:
            f.write("some archived note")
        self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "archive", "2023", "old-notes.txt")
        self.assertTrue(os.path.exists(dst))
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), "some archived note")
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_conflicting_file_in_nested_subdir_preserves_both(self):
        nested_dst_dir = os.path.join(self.mod.REPORTS_DIR, "sub")
        os.makedirs(nested_dst_dir, exist_ok=True)
        with open(os.path.join(nested_dst_dir, "XYZ-1.md"), "w", encoding="utf-8") as f:
            f.write("new content")
        nested_src_dir = os.path.join(self.legacy_desktop, "sub")
        os.makedirs(nested_src_dir)
        with open(os.path.join(nested_src_dir, "XYZ-1.md"), "w", encoding="utf-8") as f:
            f.write("different legacy content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        with open(os.path.join(nested_dst_dir, "XYZ-1.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new content")
        conflict_path = os.path.join(nested_dst_dir, "XYZ-1.legacy-conflict.md")
        self.assertTrue(os.path.exists(conflict_path))
        with open(conflict_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "different legacy content")
        self.assertIn("conflicting", stderr_capture.getvalue())
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_symlinked_subdirectory_blocks_removal_and_is_never_followed(self):
        outside = os.path.join(self.tmp, "outside-dir")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("outside content")
        link = os.path.join(self.legacy_desktop, "linked")
        os.symlink(outside, link, target_is_directory=True)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "linked", "secret.txt")
        self.assertFalse(os.path.exists(dst), "a symlinked directory must never be followed/migrated")
        self.assertTrue(os.path.exists(self.legacy_desktop), "unresolved symlink must block legacy dir removal")
        self.assertIn("symlink", stderr_capture.getvalue())

    def test_repeated_runs_blocked_by_symlink_never_mint_new_conflict_files(self):
        # Regression: a legacy dir that stays around across multiple runs
        # (blocked from removal by an unrelated unresolved item, e.g. a
        # symlink) must not accumulate new .legacy-conflict-N files for
        # content that's already been preserved by a previous run.
        os.makedirs(self.mod.REPORTS_DIR, exist_ok=True)
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), "w", encoding="utf-8") as f:
            f.write("new content")
        self._write_legacy_file("ABC-1.md", "different legacy content")
        # A symlinked subdirectory that can never be resolved automatically
        # keeps the legacy dir from ever being removed across runs.
        outside = os.path.join(self.tmp, "outside-dir")
        os.makedirs(outside)
        link = os.path.join(self.legacy_desktop, "linked")
        os.symlink(outside, link, target_is_directory=True)

        for _ in range(3):
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                self.mod.migrate_legacy_desktop_reports()
            self.assertTrue(os.path.exists(self.legacy_desktop), "symlink must keep blocking removal")

        entries = sorted(os.listdir(self.mod.REPORTS_DIR))
        self.assertEqual(
            entries,
            ["ABC-1.legacy-conflict.md", "ABC-1.md"],
            "repeated runs must never create ABC-1.legacy-conflict-2.md, -3.md, etc. "
            "for content already preserved",
        )
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "different legacy content")
        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "new content")

    # --- Finding 1: overlapping REPORTS_DIR/LEGACY_DESKTOP_DIR (nested,
    # parent, equal, or via symlink) must never trigger deletion/copy. ---

    def test_overlap_equal_paths_after_realpath_blocks_migration(self):
        self.mod.REPORTS_DIR = self.legacy_desktop
        self._write_legacy_file("ABC-1.md", "content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        self.assertTrue(os.path.exists(self.legacy_desktop))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_desktop, "ABC-1.md")))
        self.assertIn("overlaps", stderr_capture.getvalue())

    def test_overlap_reports_dir_nested_inside_legacy_dir_blocks_migration(self):
        # REPORTS_DIR is a subdirectory OF the legacy dir.
        self.mod.REPORTS_DIR = os.path.join(self.legacy_desktop, "nested-reports")
        self._write_legacy_file("ABC-1.md", "content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        self.assertTrue(os.path.exists(self.legacy_desktop))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_desktop, "ABC-1.md")))
        self.assertIn("overlaps", stderr_capture.getvalue())

    def test_overlap_legacy_dir_nested_inside_reports_dir_blocks_migration(self):
        # LEGACY_DESKTOP_DIR is a subdirectory OF REPORTS_DIR.
        self.mod.REPORTS_DIR = self.tmp
        self.mod.LEGACY_DESKTOP_DIR = os.path.join(self.tmp, "CopilotJiraTaskReports")
        os.makedirs(self.mod.LEGACY_DESKTOP_DIR, exist_ok=True)
        with open(os.path.join(self.mod.LEGACY_DESKTOP_DIR, "ABC-1.md"), "w", encoding="utf-8") as f:
            f.write("content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        self.assertTrue(os.path.exists(self.mod.LEGACY_DESKTOP_DIR))
        self.assertTrue(os.path.exists(os.path.join(self.mod.LEGACY_DESKTOP_DIR, "ABC-1.md")))
        self.assertIn("overlaps", stderr_capture.getvalue())

    def test_overlap_via_symlinked_reports_dir_blocks_migration(self):
        # REPORTS_DIR is itself a symlink that resolves INTO the legacy
        # dir; realpath must catch this even though naive abspath
        # comparison (or comparing un-resolved paths) would not.
        real_reports = os.path.join(self.legacy_desktop, "actual-reports")
        os.makedirs(real_reports)
        self.mod.REPORTS_DIR = os.path.join(self.tmp, "reports-link")
        os.symlink(real_reports, self.mod.REPORTS_DIR, target_is_directory=True)
        self._write_legacy_file("ABC-1.md", "content")
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        self.assertTrue(os.path.exists(self.legacy_desktop))
        self.assertTrue(os.path.exists(os.path.join(self.legacy_desktop, "ABC-1.md")))
        self.assertIn("overlaps", stderr_capture.getvalue())

    # --- Finding 2: repeated blocked runs must compare against the
    # TRANSFORMED expected content, not just raw source bytes, so a
    # correctly-migrated destination is never re-flagged as conflicting
    # (and never gets a stale Jira heading re-minted into a conflict copy). ---

    def test_repeated_run_against_already_transformed_destination_is_not_a_conflict(self):
        task_data = {"task_id": "ABC-1", "sessions": ["sess-1"]}
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(task_data))
        self._write_legacy_file(
            "ABC-1.md",
            "# Copilot Jira Usage Report — ABC-1\n\nSome stale totals: $0.00\n",
        )
        # An unrelated symlinked entry blocks legacy dir removal (a real
        # failure, unlike a conflict) even though ABC-1.md itself
        # migrates cleanly this run.
        outside = os.path.join(self.tmp, "outside-dir")
        os.makedirs(outside)
        link = os.path.join(self.legacy_desktop, "linked")
        os.symlink(outside, link, target_is_directory=True)

        for _ in range(3):
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                self.mod.migrate_legacy_desktop_reports()
            self.assertTrue(os.path.exists(self.legacy_desktop), "unrelated conflict must keep blocking removal")
            # ABC-1.md must never be reported as a conflict on repeat runs,
            # and no ABC-1.legacy-conflict.md must ever be minted.
            self.assertNotIn("ABC-1", stderr_capture.getvalue())
            self.assertFalse(
                os.path.exists(os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md")),
                "already-transformed destination must never be treated as a fresh conflict",
            )

        with open(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md"), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("Jira", content, "heading must remain normalized, not reverted to stale wording")

    # --- Finding 5: a symlinked LEGACY_DESKTOP_DIR must never be followed;
    # if it resolves to REPORTS_DIR, only the symlink itself is removed. ---

    def test_symlinked_legacy_dir_resolving_to_reports_dir_unlinks_only_symlink(self):
        real_reports = os.path.join(self.tmp, "real-reports")
        os.makedirs(real_reports)
        with open(os.path.join(real_reports, "kept.md"), "w", encoding="utf-8") as f:
            f.write("must survive untouched")
        self.mod.REPORTS_DIR = real_reports
        shutil.rmtree(self.legacy_desktop)
        os.symlink(real_reports, self.legacy_desktop, target_is_directory=True)
        self.mod.migrate_legacy_desktop_reports()
        self.assertFalse(os.path.exists(self.legacy_desktop), "the symlink itself must be removed")
        # The real target directory (== REPORTS_DIR) and its content must
        # be completely untouched.
        self.assertTrue(os.path.exists(real_reports))
        with open(os.path.join(real_reports, "kept.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "must survive untouched")

    def test_symlinked_legacy_dir_resolving_elsewhere_is_never_followed_or_deleted(self):
        outside = os.path.join(self.tmp, "unrelated-target")
        os.makedirs(outside)
        with open(os.path.join(outside, "private.txt"), "w", encoding="utf-8") as f:
            f.write("must never be read or copied")
        shutil.rmtree(self.legacy_desktop)
        os.symlink(outside, self.legacy_desktop, target_is_directory=True)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        # Symlink left in place entirely (never unlinked, since it does
        # not resolve to REPORTS_DIR); target never followed/copied.
        self.assertTrue(os.path.islink(self.legacy_desktop))
        self.assertTrue(os.path.exists(outside), "unrelated symlink target must never be deleted")
        self.assertFalse(
            os.path.exists(os.path.join(self.mod.REPORTS_DIR, "private.txt")),
            "an unrelated symlink target must never be followed/copied into REPORTS_DIR",
        )
        self.assertIn("symlink", stderr_capture.getvalue())

    def test_nested_md_matching_task_id_is_not_fully_rerendered(self):
        # Finding 2 regression: only the canonical TOP-LEVEL <task-id>.md
        # (mirroring report_path()'s flat layout) may be fully re-rendered
        # from live tasks/<id>.json state. A NESTED copy whose basename
        # happens to match a task id (e.g. a user's own "archive/" folder)
        # must never be re-rendered against CURRENT task state — it may be
        # an intentional point-in-time snapshot whose totals must survive
        # unchanged (only the known stale heading, if present, may be
        # safely replaced).
        task_data = {
            "task_id": "ABC-1",
            "sessions": ["sess-1", "sess-2", "sess-3", "sess-4", "sess-5"],
        }
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(task_data))
        archived_snapshot = (
            "# Copilot Jira Usage Report — ABC-1\n\n"
            "Archived snapshot totals: $7.00\n"
            "Sessions at time of archive: 2\n"
        )
        nested_dir = os.path.join(self.legacy_desktop, "archive")
        os.makedirs(nested_dir)
        with open(os.path.join(nested_dir, "ABC-1.md"), "w", encoding="utf-8") as f:
            f.write(archived_snapshot)

        self.mod.migrate_legacy_desktop_reports()

        dst = os.path.join(self.mod.REPORTS_DIR, "archive", "ABC-1.md")
        with open(dst, encoding="utf-8") as f:
            content = f.read()
        # Heading normalized, but every other line (in particular the
        # archived totals/session count) must survive byte-for-byte.
        self.assertNotIn("Jira", content)
        self.assertIn("Archived snapshot totals: $7.00", content)
        self.assertIn("Sessions at time of archive: 2", content)
        self.assertNotEqual(
            content,
            self.mod.render_markdown(task_data),
            "a nested/archived copy must never be replaced by a full re-render "
            "of the current live task state",
        )
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_legacy_md_reheaded_via_full_rerender_when_matching_task_json_exists(self):
        # When the matching tasks/<id>.json is available (the normal case,
        # since the support-dir migration runs first), the migrated report
        # must be fully re-rendered: generic heading AND totals sourced
        # from the current renderer/state, not just a text patch.
        task_data = {
            "task_id": "ABC-1",
            "sessions": ["sess-1", "sess-2", "sess-3"],
        }
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(task_data))
        self._write_legacy_file(
            "ABC-1.md",
            "# Copilot Jira Usage Report — ABC-1\n\nSome stale totals: $0.00\n",
        )
        self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")
        with open(dst, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, self.mod.render_markdown(task_data))
        self.assertNotIn("Jira", content)
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_legacy_md_reheaded_via_fallback_regex_when_no_matching_task_json(self):
        # Without a matching tasks/<id>.json, the migration can't safely
        # re-render totals it doesn't have authoritative source data for;
        # it must fall back to patching ONLY the known old heading line,
        # leaving every other line (in particular the totals) untouched.
        legacy_text = (
            "# Copilot Jira Usage Report — ORPHAN-9\n\n"
            "Total cost: $42.00\n"
            "Sessions: 7\n"
        )
        self._write_legacy_file("ORPHAN-9.md", legacy_text)
        self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "ORPHAN-9.md")
        with open(dst, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(
            content,
            "# Copilot Task Usage Report — ORPHAN-9\n\nTotal cost: $42.00\nSessions: 7\n",
        )
        self.assertNotIn("Jira", content)
        self.assertFalse(os.path.exists(self.legacy_desktop))

    def test_non_md_legacy_file_copied_byte_for_byte_unchanged(self):
        content = b"\x00binary or non-markdown content that must never be rewritten"
        os.makedirs(self.legacy_desktop, exist_ok=True)
        with open(os.path.join(self.legacy_desktop, "ABC-1.csv"), "wb") as f:
            f.write(content)
        self.mod.migrate_legacy_desktop_reports()
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.csv")
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), content)

    # --- Desktop migration provenance: a top-level report already
    # produced by migration must be recognized as already-migrated even
    # once LIVE task JSON/report state has moved on, for as long as the
    # legacy directory remains blocked from removal by something else. ---

    def test_provenance_survives_live_drift_while_blocked_and_final_run_cleans_up(self):
        task_data = {"task_id": "ABC-1", "sessions": ["sess-1"]}
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(task_data))
        self._write_legacy_file(
            "ABC-1.md",
            "# Copilot Jira Usage Report — ABC-1\n\nSome stale totals: $0.00\n",
        )

        # A symlinked entry is a genuine, unrelated blocker that keeps the
        # legacy directory from ever being removed automatically.
        outside = os.path.join(self.tmp, "outside-dir")
        os.makedirs(outside)
        blocker = os.path.join(self.legacy_desktop, "linked")
        os.symlink(outside, blocker, target_is_directory=True)

        # First run: ABC-1.md migrates cleanly (full re-render from live
        # task JSON), but the blocker keeps the legacy dir in place.
        self.mod.migrate_legacy_desktop_reports()
        self.assertTrue(os.path.exists(self.legacy_desktop), "blocker must keep the legacy dir in place")
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")
        first_migrated_content = self.mod.render_markdown(task_data)
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), first_migrated_content)

        # Now simulate ordinary, unrelated LIVE usage moving the task (and
        # its report) forward: both the task JSON and its current report
        # change completely independently of the migration.
        live_task_data = {"task_id": "ABC-1", "sessions": ["sess-1", "sess-2", "sess-3"]}
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(live_task_data))
        live_report_content = self.mod.render_markdown(live_task_data) + "\n<!-- live update marker -->\n"
        with open(dst, "w", encoding="utf-8") as f:
            f.write(live_report_content)

        # Re-run the migration helper 2-3 more times while the legacy
        # directory is still blocked from removal.
        for _ in range(3):
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):
                self.mod.migrate_legacy_desktop_reports()
            stderr_text = stderr_capture.getvalue()
            self.assertNotIn(
                "ABC-1", stderr_text,
                "an already-migrated top-level entry must never be re-warned about, "
                "even after its live report/task JSON have moved on",
            )
            self.assertTrue(
                os.path.exists(self.legacy_desktop),
                "legacy dir must remain in place solely due to the unrelated blocker",
            )
            self.assertFalse(
                os.path.exists(os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md")),
                "must never create .legacy-conflict-N files for an already-migrated entry",
            )
            self.assertFalse(
                os.path.exists(os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict-2.md")),
                "must never create .legacy-conflict-N files for an already-migrated entry",
            )
            # The live, current report must be retained byte-for-byte —
            # never re-rendered/overwritten by the migration helper.
            with open(dst, encoding="utf-8") as f:
                self.assertEqual(f.read(), live_report_content)

        # Remove the blocker: the final run must now be able to remove the
        # (now-empty) legacy directory entirely, WITHOUT touching the live
        # current report along the way.
        os.remove(blocker)
        self.mod.migrate_legacy_desktop_reports()
        self.assertFalse(os.path.exists(self.legacy_desktop), "legacy dir must be removed once unblocked")
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(
                f.read(), live_report_content,
                "removing the blocker must never cause the live current report to be "
                "overwritten by a stale re-render",
            )

    def test_restored_legacy_dir_after_full_removal_is_treated_fresh_not_silently_dropped(self):
        # 1) Migrate one top-level report cleanly; the legacy dir (and its
        #    provenance marker) must be fully removed on success.
        self._write_legacy_file(
            "ABC-1.md",
            "# Copilot Jira Usage Report — ABC-1\n\nSome stale totals: $0.00\n",
        )
        self.mod.migrate_legacy_desktop_reports()
        self.assertFalse(os.path.exists(self.legacy_desktop))
        marker_path = os.path.join(self.mod.SUPPORT_DIR, self.mod.DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME)
        self.assertFalse(
            os.path.exists(marker_path),
            "the provenance marker must be pruned the moment its legacy source is fully removed",
        )

        # 2) Live usage diverges the migrated report afterward.
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")
        with open(dst, "w", encoding="utf-8") as f:
            f.write("live report content, changed after migration\n")

        # 3) The legacy directory is later RESTORED (e.g. from a backup)
        #    with the exact same original raw bytes migrated in step 1.
        #    Because the marker no longer exists, this must be treated as
        #    a brand-new occurrence: a genuine conflict against the now-
        #    diverged destination, never silently skipped as a no-op.
        os.makedirs(self.legacy_desktop, exist_ok=True)
        self._write_legacy_file(
            "ABC-1.md",
            "# Copilot Jira Usage Report — ABC-1\n\nSome stale totals: $0.00\n",
        )

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()
        self.assertIn(
            "conflicting",
            stderr_capture.getvalue(),
            "a restored legacy source must be freshly compared/flagged, never silently dropped "
            "via a stale marker fingerprint",
        )
        conflict_path = os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md")
        self.assertTrue(os.path.exists(conflict_path), "the restored legacy content must be preserved")
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(
                f.read(),
                "live report content, changed after migration\n",
                "live data must remain untouched by the restored legacy copy",
            )

    def test_differing_raw_legacy_content_preserved_as_conflict_even_when_rerender_matches_dst(self):
        # Finding 2 regression: a canonical top-level destination is, by
        # construction, normally kept in sync with the CURRENT live task
        # state. If the migration compared a legacy source against a full
        # live re-render of that SAME state (rather than the raw legacy
        # source itself), a genuinely different historical legacy record
        # would spuriously "match" and be silently dropped with no
        # conflict trace whatsoever. This must never happen.
        task_data = {"task_id": "ABC-1", "sessions": ["sess-1", "sess-2"]}
        self.mod.atomic_write(self.mod.task_path("ABC-1"), json.dumps(task_data))

        # dst already holds an ordinary live render of the CURRENT task
        # state (as if produced by everyday app usage, not by a previous
        # migration run).
        dst = os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")
        live_render = self.mod.render_markdown(task_data)
        os.makedirs(self.mod.REPORTS_DIR, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(live_render)

        # The legacy source is a genuinely different historical snapshot
        # with its own unique, distinguishing total.
        unique_total = "999.99"
        legacy_text = (
            "# Copilot Jira Usage Report — ABC-1\n\n"
            "Historical total at time of report: $%s\n" % unique_total
        )
        self._write_legacy_file("ABC-1.md", legacy_text)

        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.mod.migrate_legacy_desktop_reports()

        self.assertIn(
            "ABC-1",
            stderr_capture.getvalue(),
            "a raw legacy source differing from the live-rendered destination must be flagged as "
            "a conflict, never silently swallowed just because a re-render of live state happens "
            "to equal dst",
        )
        # The live destination must be completely untouched.
        with open(dst, encoding="utf-8") as f:
            self.assertEqual(f.read(), live_render)
        # The unique historical legacy content must survive, verbatim
        # (heading-only normalized), in the conflict copy.
        conflict_path = os.path.join(self.mod.REPORTS_DIR, "ABC-1.legacy-conflict.md")
        self.assertTrue(os.path.exists(conflict_path))
        with open(conflict_path, encoding="utf-8") as f:
            conflict_content = f.read()
        self.assertIn(unique_total, conflict_content, "the unique historical total must survive in the conflict copy")
        self.assertNotIn("Jira", conflict_content, "heading may still be safely normalized")
        self.assertNotEqual(
            conflict_content,
            live_render,
            "the conflict copy must preserve the raw legacy record, never a re-render of live state",
        )

    def test_empty_nested_legacy_subdirectory_is_preserved_at_destination(self):
        # Finding 3 regression: a directory containing no files anywhere
        # in its own subtree must still get a destination created for it,
        # rather than being silently lost once the legacy tree is
        # removed (previously only file writes created directories).
        empty_dir = os.path.join(self.legacy_desktop, "empty-archive")
        os.makedirs(empty_dir)
        nested_empty_dir = os.path.join(self.legacy_desktop, "outer", "inner-empty")
        os.makedirs(nested_empty_dir)

        self.mod.migrate_legacy_desktop_reports()

        self.assertTrue(
            os.path.isdir(os.path.join(self.mod.REPORTS_DIR, "empty-archive")),
            "an empty legacy subdirectory must be preserved (created) at the destination",
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.mod.REPORTS_DIR, "outer", "inner-empty")),
            "a nested empty legacy subdirectory must be preserved (created) at the destination",
        )
        self.assertFalse(os.path.exists(self.legacy_desktop))


class TestRunStartupMigrationsIsSafeAndIdempotent(BaseTestCase):
    """run_startup_migrations() drives both migrations under the ingest
    lock; must be a safe no-op to call repeatedly, and on every normal
    ingest/report invocation, once the legacy sources are gone."""

    def setUp(self):
        super().setUp()
        self.mod.LEGACY_SUPPORT_DIR = os.path.join(self.tmp, "jira-reports")
        self.mod.LEGACY_DESKTOP_DIR = os.path.join(self.tmp, "Desktop", "CopilotJiraTaskReports")

    def test_repeated_calls_with_no_legacy_sources_are_noops(self):
        self.mod.run_startup_migrations()
        self.mod.run_startup_migrations()
        self.mod.run_startup_migrations()
        self.assertFalse(os.path.exists(self.mod.LEGACY_SUPPORT_DIR))
        self.assertFalse(os.path.exists(self.mod.LEGACY_DESKTOP_DIR))

    def test_migrates_both_legacy_sources_in_one_call(self):
        os.remove(self.mod.PRICING_FILE)  # BaseTestCase seeds a fixture; remove for this "absent" case
        os.makedirs(self.mod.LEGACY_SUPPORT_DIR)
        with open(os.path.join(self.mod.LEGACY_SUPPORT_DIR, "model-pricing.json"), "w") as f:
            json.dump({"updated_at": "legacy", "models": {}, "aliases": {}}, f)
        os.makedirs(self.mod.LEGACY_DESKTOP_DIR)
        with open(os.path.join(self.mod.LEGACY_DESKTOP_DIR, "ABC-1.md"), "w") as f:
            f.write("legacy report")

        self.mod.run_startup_migrations()

        self.assertFalse(os.path.exists(self.mod.LEGACY_SUPPORT_DIR))
        self.assertFalse(os.path.exists(self.mod.LEGACY_DESKTOP_DIR))
        with open(self.mod.PRICING_FILE) as f:
            self.assertEqual(json.load(f)["updated_at"], "legacy")
        self.assertTrue(os.path.exists(os.path.join(self.mod.REPORTS_DIR, "ABC-1.md")))

    def test_both_migrations_run_inside_a_single_shared_lock_scope(self):
        # Both migrations must run under the SAME cross-process lock
        # acquisition (as documented on run_startup_migrations), not each
        # wrap its own separate lock — otherwise a naive nested
        # `with ingest_lock():` inside either migration would deadlock
        # against the already-held outer lock (flock is per-open-file-
        # description, so a second, independent open() on the same lock
        # file from the same process would block forever).
        events = []
        lock_depth = [0]

        @contextlib.contextmanager
        def fake_lock():
            lock_depth[0] += 1
            events.append(("enter", lock_depth[0]))
            try:
                yield
            finally:
                events.append(("exit", lock_depth[0]))
                lock_depth[0] -= 1

        real_task_storage = self.mod.migrate_legacy_task_storage
        real_desktop_reports = self.mod.migrate_legacy_desktop_reports

        def spy_task_storage():
            events.append(("task_storage_called", lock_depth[0]))
            return real_task_storage()

        def spy_desktop_reports():
            events.append(("desktop_reports_called", lock_depth[0]))
            return real_desktop_reports()

        self.mod.ingest_lock = fake_lock
        self.mod.migrate_legacy_task_storage = spy_task_storage
        self.mod.migrate_legacy_desktop_reports = spy_desktop_reports
        try:
            self.mod.run_startup_migrations()
        finally:
            self.mod.migrate_legacy_task_storage = real_task_storage
            self.mod.migrate_legacy_desktop_reports = real_desktop_reports

        # Exactly one lock acquisition for the whole call...
        enters = [e for e in events if e[0] == "enter"]
        exits = [e for e in events if e[0] == "exit"]
        self.assertEqual(len(enters), 1)
        self.assertEqual(len(exits), 1)
        # ...and both migrations must have been invoked while that single
        # acquisition was still active (lock_depth == 1), i.e. genuinely
        # nested inside it rather than acquiring/releasing their own.
        calls = [e for e in events if e[0].endswith("_called")]
        self.assertEqual(len(calls), 2)
        for _label, depth_at_call in calls:
            self.assertEqual(depth_at_call, 1)


class TestReportAndTaskCliParity(BaseTestCase):
    """The `report` subcommand accepts a positional task ID/name argument;
    the surrounding CLI (via bin/copilot-s's --report/--task) must resolve
    to the exact same underlying call — verified here at the argparse
    level using main()'s actual parser."""

    def _run_cli(self, argv):
        parser_main = self.mod.main
        old_argv = sys.argv
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            sys.argv = ["copilot-task-report.py"] + argv
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                try:
                    parser_main()
                    rc = 0
                except SystemExit as ex:
                    rc = ex.code if isinstance(ex.code, int) else (0 if ex.code is None else 1)
        finally:
            sys.argv = old_argv
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_ingest_accepts_task_flag(self):
        session_id = "sess-cli-task"
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", t, t + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=1, completion=1)),
        ])
        rc, _out, _err = self._run_cli([
            "ingest", "--session-id", session_id,
            "--task", "ABC-1",
            "--session-dir", os.path.join(self.mod.SESSION_STATE_DIR, session_id),
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.task("ABC-1")["task_id"], "ABC-1")

    def test_report_positional_arg_reports_on_matching_task(self):
        self.mod.atomic_write(
            self.mod.task_path("ABC-2"),
            json.dumps({"task_id": "ABC-2", "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
        )
        rc, out, _err = self._run_cli(["report", "ABC-2"])
        self.assertEqual(rc, 0)
        self.assertIn("ABC-2", out)

    def test_no_jira_flag_recognized_anywhere_in_cli(self):
        # The legacy --jira flag must no longer be recognized at all: the
        # parser must reject it as an unrecognized argument.
        rc, _out, err = self._run_cli([
            "ingest", "--session-id", "sess-x", "--jira", "ABC-1",
        ])
        self.assertNotEqual(rc, 0)
        self.assertIn("unrecognized", err.lower())

    def test_normalize_rejects_leading_dash_task_id(self):
        rc, _out, err = self._run_cli(["normalize-task-id", "--", "-rf"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a valid task id", err.lower())

    def test_ingest_with_task_equals_form_safely_falls_back_on_leading_dash(self):
        # `--task=<value>` must be parsed correctly by argparse even when
        # <value> itself starts with '-' (never misread as a second,
        # unrelated flag) — and the leading-dash value must then be
        # rejected by normalize_task_id() and safely fall back to
        # UNASSIGNED rather than ever being used as a literal task id
        # (which could otherwise resemble a CLI flag to some other tool).
        session_id = "sess-cli-dash"
        session_dir = os.path.join(self.mod.SESSION_STATE_DIR, session_id)
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", t, t + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=1, completion=1)),
        ])
        rc, _out, err = self._run_cli([
            "ingest", "--session-id", session_id,
            "--task=--rm-rf",
            "--session-dir", session_dir,
        ])
        self.assertEqual(rc, 0)
        self.assertIn("invalid task ID", err)
        self.assertFalse(os.path.exists(self.mod.task_path("--rm-rf")))
        self.assertEqual(self.task(self.mod.UNASSIGNED)["task_id"], self.mod.UNASSIGNED)

    def test_report_rejects_leading_dash_task_id(self):
        rc, _out, err = self._run_cli(["report", "--", "-rf"])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a valid task id", err.lower())

    def test_normalize_rejects_overlong_key_style_task_id(self):
        overlong_key = "A" * (self.mod.MAX_TASK_ID_LEN + 5) + "-123"
        rc, _out, err = self._run_cli(["normalize-task-id", "--", overlong_key])
        self.assertNotEqual(rc, 0)
        self.assertIn("not a valid task id", err.lower())


class TestNoLegacyEnvVarsHaveEffect(BaseTestCase):
    """COPILOT_JIRA_* env vars must have zero effect: the new module only
    reacts to COPILOT_TASK_REPORTS_DIR (and the caller-side
    COPILOT_TASK_REPORT_HELPER, exercised in tests/test_copilot_s.py)."""

    def test_copilot_jira_reports_dir_env_var_is_ignored(self):
        # Reload the module with a legacy env var set: REPORTS_DIR must
        # resolve to the new default, never honor the old name.
        old_env = os.environ.get("COPILOT_JIRA_REPORTS_DIR")
        os.environ["COPILOT_JIRA_REPORTS_DIR"] = "/tmp/should-be-ignored"
        try:
            mod = load_module()
            self.assertNotEqual(mod.REPORTS_DIR, "/tmp/should-be-ignored")
        finally:
            if old_env is None:
                os.environ.pop("COPILOT_JIRA_REPORTS_DIR", None)
            else:
                os.environ["COPILOT_JIRA_REPORTS_DIR"] = old_env

    def test_copilot_task_reports_dir_env_var_is_honored(self):
        old_env = os.environ.get("COPILOT_TASK_REPORTS_DIR")
        os.environ["COPILOT_TASK_REPORTS_DIR"] = "/tmp/should-be-used-xyz"
        try:
            mod = load_module()
            self.assertEqual(mod.REPORTS_DIR, "/tmp/should-be-used-xyz")
        finally:
            if old_env is None:
                os.environ.pop("COPILOT_TASK_REPORTS_DIR", None)
                if os.path.exists("/tmp/should-be-used-xyz"):
                    pass
            else:
                os.environ["COPILOT_TASK_REPORTS_DIR"] = old_env


if __name__ == "__main__":
    unittest.main(verbosity=2)
