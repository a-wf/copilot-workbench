#!/usr/bin/env python3
"""
Focused regression tests for copilot-jira-report.py, covering the reviewer
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
MODULE_PATH = os.path.join(REPO_ROOT, "bin", "copilot-jira-report.py")
SCRIPT_PATH = os.path.join(REPO_ROOT, "bin", "copilot-s")
PRICING_PATH = os.path.join(REPO_ROOT, "config", "model-pricing.json")


def load_module():
    spec = importlib.util.spec_from_file_location("copilot_jira_report", MODULE_PATH)
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
        self.tmp = tempfile.mkdtemp(prefix="copilot-jira-report-test-")

        support_dir = os.path.join(self.tmp, "jira-reports")
        self.mod.SUPPORT_DIR = support_dir
        self.mod.TICKETS_DIR = os.path.join(support_dir, "tickets")
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

    def ingest(self, session_id, jira="TEST-1"):
        class Args:
            pass
        args = Args()
        args.session_id = session_id
        args.session_dir = os.path.join(self.mod.SESSION_STATE_DIR, session_id)
        args.jira = jira
        return self.mod.cmd_ingest(args)

    def ticket(self, jira="TEST-1"):
        return self.mod.load_json(self.mod.ticket_path(jira), {})


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
        t = self.ticket()
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
        t = self.ticket()
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
        totals = self.ticket()["totals"]
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
        totals = self.ticket()["totals"]
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
        totals = self.ticket()["totals"]
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
        by_effort = self.ticket()["by_effort"]
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
        by_effort = self.ticket()["by_effort"]
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
        by_effort = self.ticket()["by_effort"]
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
        by_effort = self.ticket()["by_effort"]
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

        totals = self.ticket()["totals"]
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
        totals = self.ticket()["totals"]
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
        ticket = self.ticket()
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
        totals = self.ticket()["totals"]
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

        totals = self.ticket()["totals"]
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

        totals = self.ticket()["totals"]
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

        totals = self.ticket()["totals"]
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

        totals = self.ticket()["totals"]
        self.assertEqual(totals["call_count"], 2)
        self.assertEqual(totals["prompt_tokens"], 3)
        self.assertEqual(totals["completion_tokens"], 3)


class TestCmdReportKeyValidation(BaseTestCase):
    """Finding 3: cmd_report must strictly normalize/validate the Jira key
    via normalize_jira_key(), never falling back to a raw
    strip()+upper() of caller input for a key that doesn't actually match
    the Jira key format — a malformed key must fail clearly instead of
    being looked up (or silently creating a ticket/report file) under
    whatever garbage string was passed in."""

    def report(self, jira_key):
        class Args:
            pass
        args = Args()
        args.jira_key = jira_key
        return self.mod.cmd_report(args)

    def test_malformed_key_fails_clearly_instead_of_raw_fallback(self):
        rc = self.report("not a valid key!!")
        self.assertEqual(rc, 1)
        # Must not have created/looked up a ticket/report file under the
        # raw, merely-uppercased garbage string.
        raw_upper = "NOT A VALID KEY!!"
        self.assertFalse(os.path.exists(self.mod.ticket_path(raw_upper)))
        self.assertFalse(os.path.exists(self.mod.report_path(raw_upper)))

    def test_valid_key_is_normalized_and_reported(self):
        # Seed a ticket the same way ingest would (already-normalized key).
        ticket_dir_key = "ABC-123"
        self.mod.atomic_write(
            self.mod.ticket_path(ticket_dir_key),
            json.dumps({"jira_key": ticket_dir_key, "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
        )
        # Lowercase input with incidental whitespace must still resolve to
        # the same normalized ticket.
        rc = self.report("  abc-123  ")
        self.assertEqual(rc, 0)

    def test_unassigned_literal_is_accepted(self):
        self.mod.atomic_write(
            self.mod.ticket_path(self.mod.UNASSIGNED),
            json.dumps({"jira_key": self.mod.UNASSIGNED, "totals": self.mod.blank_agg(), "by_model": {}, "by_effort": {}}),
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
        totals = self.ticket()["totals"]
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
        self.assertEqual(self.ticket("AA-1")["totals"]["prompt_tokens"], 10)
        self.assertEqual(self.ticket("BB-1")["totals"]["prompt_tokens"], 20)

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
        totals = self.ticket()["totals"]
        self.assertEqual(totals["call_count"], 2)
        self.assertEqual(totals["prompt_tokens"], 3)
        self.assertEqual(totals["completion_tokens"], 3)


class TestMultiSessionSameTicketAggregation(BaseTestCase):
    """Multiple sessions may contribute to the same Jira ticket."""

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
        t = self.ticket("AGG-1")
        self.assertEqual(sorted(t["sessions"]), ["sess-1", "sess-2"])
        self.assertEqual(t["totals"]["call_count"], 2)
        self.assertEqual(t["totals"]["prompt_tokens"], 30)
        self.assertEqual(t["totals"]["completion_tokens"], 15)


class TestReportRendering(BaseTestCase):
    """cmd_ingest must write a Markdown report with meaningful rendered
    values, and cmd_report must print that report (not just return 0)."""

    def _seed_report_data(self, session_id, jira):
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
        self.assertEqual(self.ingest(session_id, jira), 0)

    def test_cmd_ingest_writes_markdown_report(self):
        session_id = "sess-report"
        jira = "REPORT-1"
        self._seed_report_data(session_id, jira)
        rpath = self.mod.report_path(jira)
        self.assertTrue(os.path.exists(rpath), "Markdown report must be written after ingest")
        with open(rpath, encoding="utf-8") as f:
            md = f.read()
        self.assertIn("# Copilot Jira Usage Report — %s" % jira, md)
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
        jira = "REPORT-2"
        self._seed_report_data(session_id, jira)
        class Args:
            pass
        args = Args()
        args.jira_key = jira
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = self.mod.cmd_report(args)
        self.assertEqual(rc, 0)
        md = out_buf.getvalue()
        self.assertIn("# Copilot Jira Usage Report — %s" % jira, md)
        self.assertIn("claude-sonnet-5", md)
        self.assertIn("measured:high", md)
        self.assertIn("$0.0100", md)
        self.assertIn("(report file:", err_buf.getvalue())


class TestUnassignedAndInvalidJiraFallback(BaseTestCase):
    """Explicit UNASSIGNED ingest and invalid --jira must fall back safely
    to the UNASSIGNED bucket rather than creating a malformed ticket file."""

    def test_unassigned_ingest_creates_unassigned_ticket(self):
        session_id = "sess-unassigned"
        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat gpt-5.4", t, t + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=7, completion=3)),
        ])
        self.assertEqual(self.ingest(session_id, None), 0)
        t = self.ticket(self.mod.UNASSIGNED)
        self.assertEqual(t["jira_key"], self.mod.UNASSIGNED)
        self.assertEqual(t["totals"]["call_count"], 1)
        self.assertTrue(os.path.exists(self.mod.report_path(self.mod.UNASSIGNED)))

    def test_invalid_jira_fallback_to_unassigned(self):
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
        self.assertIn("ignoring invalid Jira key", stderr_capture.getvalue())
        self.assertFalse(os.path.exists(self.mod.ticket_path("NOT-A-KEY!!")))
        self.assertFalse(os.path.exists(self.mod.ticket_path("not-a-key!!")))
        t = self.ticket(self.mod.UNASSIGNED)
        self.assertEqual(t["jira_key"], self.mod.UNASSIGNED)
        self.assertEqual(t["totals"]["call_count"], 1)


class TestReportPersistenceAfterDeletion(BaseTestCase):
    """Deleting a session directory must not remove the already-ingested
    ticket/report; a later session for the same ticket must still aggregate
    on top of the persisted data."""

    def test_ticket_and_report_persist_after_session_dir_deleted(self):
        jira = "PERSIST-1"
        s1 = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span("sess-persist-a", "chat gpt-5.4", s1, s1 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=10, completion=5)),
        ])
        self.ingest("sess-persist-a", jira)
        self.assertTrue(os.path.exists(self.mod.ticket_path(jira)))
        self.assertTrue(os.path.exists(self.mod.report_path(jira)))

        # Simulate the user deleting the session state directory.
        shutil.rmtree(os.path.join(self.mod.SESSION_STATE_DIR, "sess-persist-a"), ignore_errors=True)
        self.assertFalse(os.path.exists(self.events_path("sess-persist-a")))

        # Ticket/report must still be present.
        t = self.ticket(jira)
        self.assertIn("sess-persist-a", t["sessions"])
        self.assertEqual(t["totals"]["call_count"], 1)
        self.assertTrue(os.path.exists(self.mod.report_path(jira)))

        # A new session for the same ticket aggregates on top.
        s2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span("sess-persist-b", "chat gpt-5.4", s2, s2 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=20, completion=10)),
        ])
        self.ingest("sess-persist-b", jira)
        t = self.ticket(jira)
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
    this machine (~/.copilot/jira-reports/model-pricing.json and
    ~/.local/bin/copilot-s). These are separate from the hermetic repo
    checks above: they never assume the toolkit has been installed, and
    they skip cleanly (rather than fail) when it hasn't."""

    def test_installed_pricing_json_if_present(self):
        installed_pricing = os.path.join(
            os.path.expanduser("~"), ".copilot", "jira-reports", "model-pricing.json"
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
        t = self.ticket("PRICE-1")
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
        by_effort = self.ticket()["by_effort"]
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
        self.assertIn("unknown", self.ticket()["by_effort"])


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
        totals = self.ticket()["totals"]
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
        totals = self.ticket()["totals"]
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
        self.assertEqual(self.ticket()["totals"]["call_count"], 2)
        # Truncate to just the first line, then ingest to resync.
        with open(path, "rb") as f:
            first_line = f.readline()
        with open(path, "wb") as f:
            f.write(first_line)
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.ingest(session_id)
        # The truncated second span must not be re-counted during resync.
        self.assertEqual(self.ticket()["totals"]["call_count"], 2)
        # New data appended after the resync must be counted on the next ingest.
        s3 = self.t0 + timedelta(minutes=3)
        write_jsonl(path, [
            otel_span(session_id, "chat gpt-5.4", s3, s3 + timedelta(seconds=1),
                      usage_attrs("gpt-5.4", prompt=30, completion=15)),
        ])
        self.ingest(session_id)
        totals = self.ticket()["totals"]
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
            "    '--jira', ticket,\n"
            "    '--session-dir', sess_dir], check=True)\n"
        ) % (home, MODULE_PATH, session_id, ticket, json.dumps(span))

    def test_concurrent_ingests_do_not_clobber_or_double_count(self):
        if self.mod.fcntl is None:
            self.skipTest("fcntl not available")
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".copilot", "jira-reports"), exist_ok=True)
        os.makedirs(os.path.join(home, ".copilot", "otel"), exist_ok=True)
        os.makedirs(os.path.join(home, ".copilot", "session-state"), exist_ok=True)
        # Install marker + pricing fixture for the subprocesses.
        marker = {"installed_at": iso(self.t0), "installed_epoch": self.t0.timestamp()}
        with open(os.path.join(home, ".copilot", "jira-reports", "install-marker.json"), "w") as f:
            json.dump(marker, f)
        with open(os.path.join(home, ".copilot", "jira-reports", "model-pricing.json"), "w") as f:
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

        ticket_path = os.path.join(home, ".copilot", "jira-reports", "tickets", "%s.json" % ticket)
        self.assertTrue(os.path.exists(ticket_path), "ticket file must exist after concurrent ingests")
        with open(ticket_path) as f:
            t = json.load(f)
        self.assertEqual(t["totals"]["call_count"], 2)
        self.assertEqual(t["totals"]["prompt_tokens"], 30)
        self.assertEqual(t["totals"]["completion_tokens"], 15)


class TestCopilotSShell(unittest.TestCase):
    """Shell-level tests for copilot-s: Jira extraction/normalization,
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
        helper = os.path.join(self.home, ".local", "bin", "copilot-jira-report.py")
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
    marker_path = os.path.join(HOME, ".copilot", "jira-reports", "install-marker.json")
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    with open(marker_path, "w") as f:
        f.write('{"installed_at":"2026-09-21T12:00:00Z","installed_epoch":2000000000.0}')
    sys.exit(0)
if cmd == "ingest":
    session_id = ""
    jira = ""
    session_dir = ""
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--session-id" and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--jira" and i + 1 < len(sys.argv):
            jira = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--session-dir" and i + 1 < len(sys.argv):
            session_dir = sys.argv[i + 1]; i += 2
        else:
            i += 1
    dir_exists = os.path.isdir(session_dir) if session_dir else False
    events_path = os.path.join(session_dir, "events.jsonl") if session_dir else ""
    events_exists = os.path.isfile(events_path) if session_dir else False
    write_log(f"ingest session={session_id} jira={jira} dir_exists={dir_exists} events_exists={events_exists}")
    if not dir_exists:
        print(f"helper: session dir missing at ingest time: {session_dir}", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("COPILOT_JIRA_REPORT_FAIL") == "1":
        print("helper forced failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if cmd == "report":
    key = sys.argv[2] if len(sys.argv) > 2 else ""
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
        # These tests exercise Jira-reporting *behavior* against a fake
        # helper, not helper-path *resolution* (covered separately in
        # tests/test_copilot_s.py) — so always point copilot-s at the fake
        # helper via the explicit override, regardless of where self.SCRIPT
        # happens to live relative to it (e.g. executed directly from the
        # real repo bin/, which has its own real sibling helper).
        full_env["COPILOT_JIRA_REPORT_HELPER"] = self.helper_path
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

    def test_extract_jira_from_branch(self):
        rc, out, err = self._run(self._source_prefix() + '''
extract_jira_from_branch "feature/abc-123-do-stuff" || true
extract_jira_from_branch "ABC-456" || true
extract_jira_from_branch "bugfix/lower-xyz-789" || true
extract_jira_from_branch "no-ticket-here" || echo NONE
''')
        lines = [l for l in out.strip().splitlines() if l]
        self.assertEqual(lines, ["ABC-123", "ABC-456", "XYZ-789", "NONE"])

    def test_normalize_and_validate_jira(self):
        rc, out, err = self._run(self._source_prefix() + '''
normalize_and_validate_jira "  abc-123  " || true
normalize_and_validate_jira "ABC-123" || true
normalize_and_validate_jira "unassigned" || echo INVALID
normalize_and_validate_jira "bad-key" || echo INVALID
''')
        lines = [l for l in out.strip().splitlines() if l]
        # The bash normalizer validates the Jira-key shape only; the
        # UNASSIGNED sentinel is handled by resolve_jira_key, not here.
        self.assertEqual(lines, ["ABC-123", "ABC-123", "INVALID", "INVALID"])

    def test_resolve_jira_key_from_current_branch(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature/PROJ-123-desc"], cwd=repo, check=True)
        rc, out, err = self._run(self._source_prefix() + 'resolve_jira_key ""', cwd=repo)
        self.assertEqual(out.strip(), "PROJ-123")

    def test_resolve_jira_key_from_stored_branch(self):
        session_dir = os.path.join(self.home, ".copilot", "session-state", "sess-store")
        os.makedirs(session_dir)
        with open(os.path.join(session_dir, "workspace.yaml"), "w") as f:
            f.write("branch: feature/store-456-stuff\n")
        rc, out, err = self._run(self._source_prefix() + 'resolve_jira_key "sess-store"')
        self.assertEqual(out.strip(), "STORE-456")

    def test_resolve_jira_key_unassigned_on_empty_input(self):
        rc, out, err = self._run(self._source_prefix() + 'resolve_jira_key ""', stdin_text="\n")
        self.assertEqual(out.strip(), "UNASSIGNED")
        self.assertIn("No Jira ticket detected", err)

    def test_resolve_jira_key_invalid_three_times_defaults_unassigned(self):
        rc, out, err = self._run(
            self._source_prefix() + 'resolve_jira_key ""',
            stdin_text="bad\nalso-bad\nstill-bad\n",
        )
        self.assertEqual(out.strip(), "UNASSIGNED")
        self.assertIn("Too many invalid attempts", err)

    def test_resolve_jira_key_valid_input_normalized(self):
        rc, out, err = self._run(
            self._source_prefix() + 'resolve_jira_key ""',
            stdin_text="  myticket-42  \n",
        )
        self.assertEqual(out.strip(), "MYTICKET-42")

    def test_update_jira_report_is_nonfatal_on_helper_failure(self):
        # The helper must see a present session dir so it can proceed to
        # the forced-failure path and prove update_jira_report is nonfatal.
        self._make_session_dir("sess-fail")
        rc, out, err = self._run(
            self._source_prefix() + 'update_jira_report "sess-fail" "TEST-1"',
            env={"COPILOT_JIRA_REPORT_FAIL": "1"},
        )
        self.assertEqual(rc, 0)
        self.assertIn("Jira usage report update failed", err)
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
        self.assertIn("ingest session=%s jira=KEEP-1 dir_exists=True" % sid, log)
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
        self.assertIn("ingest session=%s jira=RENAME-1 dir_exists=True" % sid, log)
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
        self.assertIn("ingest session=%s jira=DELETE-1 dir_exists=True events_exists=True" % sid, log)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", sid)))

    def test_multi_delete_ingests_before_deletion(self):
        # Set up two sessions with stored branches so noninteractive
        # resolution finds a Jira key.
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
        self.assertIn("ingest session=del-a jira=PROJ-1 dir_exists=True events_exists=False", log)
        self.assertIn("ingest session=del-b jira=PROJ-2 dir_exists=True events_exists=False", log)
        # Session directories must be gone.
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", "del-a")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot", "session-state", "del-b")))
        # Sessions file rewritten without deleted IDs.
        with open(sessions_file) as f:
            remaining = f.read()
        self.assertNotIn("del-a", remaining)
        self.assertNotIn("del-b", remaining)


if __name__ == "__main__":
    unittest.main(verbosity=2)
