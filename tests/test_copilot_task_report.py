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
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import ssl
import urllib.error
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "bin", "copilot-task-report.py")
SCRIPT_PATH = os.path.join(REPO_ROOT, "bin", "copilot-s")
PRICING_PATH = os.path.join(REPO_ROOT, "config", "model-pricing.json")
REQUEST_PRICING_PATH = os.path.join(REPO_ROOT, "config", "request-pricing.json")


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


# Small, offline fixture using the official article's Markdown section,
# provider headings, table headers, and row shapes. Test values are fixed so
# parser/price calculations never depend on docs.github.com or on installed
# user pricing files.
OFFICIAL_PRICING_MARKDOWN = """\
## Pricing tables

Prices are in USD per 1 million tokens.

### OpenAI
| Model | Release status | Category | Tier | Threshold (input tokens) | Input | Cached input | Cache write | Output |
|---|---|---|---|---|---|---|---|---|
| GPT-6 Luna | GA | Chat | Default | Not applicable | $0.1 | $0.01 | $0.125 | $0.5 |
| GPT-6.1 Sol | GA | Chat | Default | ≤ 272K | $2 | $0.1 | $2.5 | $10 |
| GPT-6.1 Sol | GA | Chat | Long context | > 272K | $4 | $0.2 | $5 | $15 |

### Anthropic
| Model | Release status | Category | Tier | Threshold (input tokens) | Input | Cached input | Cache write | Output |
|---|---|---|---|---|---|---|---|---|
| Claude Opus 4 | GA | Chat | Default | ≤ 272K | $4 | $0.2 | Not applicable | $20 |
| Claude Opus 4 | GA | Chat | Long context | > 272K | $4 | $0.2 | Not applicable | $15 |
| Claude Sonnet 5 | GA | Chat | Default | Not applicable | $3 | $0.3 | Not applicable | $15 |

### Google
| Model | Release status | Category | Input | Cached input | Output |
|---|---|---|---|---|---|
| Gemini 3.8 Flash | GA | Chat | $0.075 | $0.01 | $0.3 |

### DeepSeek
| Model | Release status | Category | Input | Cached input | Cache write | Output |
|---|---|---|---|---|---|---|
| DeepSeek V4 | GA | Chat | $0.5 | $0.1 | $0.75 | $2 |

### Moonshot
| Model | Release status | Category | Input | Cached input | Cache write | Output |
|---|---|---|---|---|---|---|
| Kimi K3 | GA | Chat | $1 | $0.2 | $1.25 | $5 |

### xAI
| Model | Release status | Category | Input | Cached input | Output |
|---|---|---|---|---|---|
| Grok 4.7 | GA | Chat | $2 | $0.2 | $10 |

### Meta
| Model | Release status | Category | Input | Cached input | Output |
|---|---|---|---|---|---|
| Llama 4 | GA | Chat | $0.2 | $0.02 | $1 |

### Mistral AI
| Model | Release status | Category | Input | Cached input | Output |
|---|---|---|---|---|---|
| Mistral Large | GA | Chat | $1 | $0.1 | $4 |
"""


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
        self._old_pricing_fetch = os.environ.get("COPILOT_TASK_REPORT_PRICING_FETCH")
        os.environ["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"
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
        self.mod.PRICING_FETCHER = lambda *_args: self.fail(
            "unexpected pricing fetch; tests must inject a fixture transport"
        )
        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
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
        if self._old_pricing_fetch is None:
            os.environ.pop("COPILOT_TASK_REPORT_PRICING_FETCH", None)
        else:
            os.environ["COPILOT_TASK_REPORT_PRICING_FETCH"] = self._old_pricing_fetch

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
        return self.mod.load_json(self.mod.task_path(self.mod.normalize_task_id(task) or task), {})

    def write_request_pricing(self, data=None):
        """Write request-pricing input only under this test's temp support dir."""
        if data is None:
            with open(REQUEST_PRICING_PATH, encoding="utf-8") as f:
                data = json.load(f)
        path = self.mod.request_pricing_path()
        self.mod.atomic_write(path, json.dumps(data, indent=2))
        return path

    def write_request_pricing_raw(self, text):
        """Write raw request-pricing text only under this test's temp dir."""
        path = self.mod.request_pricing_path()
        self.mod.atomic_write(path, text)
        return path

    def official_snapshot(self, fetched_epoch=None, markdown=OFFICIAL_PRICING_MARKDOWN):
        if fetched_epoch is None:
            fetched_epoch = time.time()
        return self.mod.build_official_snapshot(markdown.encode("utf-8"), fetched_epoch)

    def write_official_snapshot(self, fetched_epoch=None, markdown=OFFICIAL_PRICING_MARKDOWN):
        snapshot = self.official_snapshot(fetched_epoch, markdown)
        self.mod.atomic_write(
            self.mod.official_pricing_cache_path(),
            json.dumps(snapshot, indent=2, sort_keys=True),
        )
        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
        return snapshot

    def use_fake_pricing_fetcher(self, result=OFFICIAL_PRICING_MARKDOWN.encode("utf-8")):
        os.environ["COPILOT_TASK_REPORT_PRICING_FETCH"] = "1"
        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
        if isinstance(result, Exception):
            def fetcher(*_args):
                raise result
        elif callable(result):
            fetcher = result
        else:
            def fetcher(*_args):
                return result
        self.mod.PRICING_FETCHER = fetcher


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


class TestModelCallAttribution(BaseTestCase):
    """Model-call attribution stays conservative while the invocation
    registry remains a separate, session-scoped source of observations."""

    def test_calls_inside_overlapping_agent_windows_remain_unknown(self):
        session_id = "sess-attribution-overlap"
        agent_start = self.t0 + timedelta(minutes=1)
        first_call = self.t0 + timedelta(minutes=2)
        second_call = self.t0 + timedelta(minutes=3)
        agent_end = self.t0 + timedelta(minutes=4)
        write_jsonl(self.events_path(session_id), [
            {
                "type": "subagent.started",
                "agentId": "agent-coder",
                "timestamp": iso(agent_start),
                "data": {"agentName": "coder"},
            },
            {
                "type": "subagent.started",
                "agentId": "agent-reviewer",
                "timestamp": iso(agent_start),
                "data": {"agentName": "reviewer"},
            },
            {
                "type": "subagent.completed",
                "agentId": "agent-coder",
                "timestamp": iso(agent_end),
                "data": {"agentName": "coder", "model": "same-model",
                         "totalTokens": 900, "durationMs": 7000},
            },
            {
                "type": "subagent.completed",
                "agentId": "agent-reviewer",
                "timestamp": iso(agent_end),
                "data": {"agentName": "reviewer", "model": "same-model",
                         "totalTokens": 800, "durationMs": 6000},
            },
            # A completion without its event-provided ID remains a
            # self-reported summary only; it cannot create a registry ID.
            {
                "type": "subagent.completed",
                "timestamp": iso(agent_end),
                "data": {"agentName": "task", "model": "same-model",
                         "totalTokens": 700, "durationMs": 5000},
            },
        ])
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id, "chat same-model", first_call,
                first_call + timedelta(seconds=1),
                usage_attrs("same-model", prompt=7, completion=3),
            ),
            otel_span(
                session_id, "chat same-model", second_call,
                second_call + timedelta(seconds=1),
                usage_attrs("same-model", prompt=5, completion=5),
            ),
        ])

        self.assertEqual(self.ingest(session_id, "ATTR-OVERLAP-1"), 0)
        task = self.task("ATTR-OVERLAP-1")
        summary = self.mod.summarize_attribution(task)
        reason = self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK

        self.assertEqual(task["totals"]["call_count"], 2)
        self.assertEqual(task["totals"]["total_tokens"], 20)
        self.assertEqual(summary["unknown"][reason], {"calls": 2, "total_tokens": 20})
        self.assertEqual(summary["attributed_calls"], 0)
        self.assertEqual(summary["attributed_tokens"], 0)
        self.assertTrue(summary["reconciled"])
        self.assertEqual(summary["registry_invocations"], 2)
        self.assertEqual(task["by_agent"]["coder"]["total_tokens"], 900)
        self.assertEqual(task["by_agent"]["reviewer"]["total_tokens"], 800)
        self.assertEqual(task["by_agent"]["task"]["total_tokens"], 700)
        self.assertNotEqual(task["by_agent"]["coder"]["total_tokens"],
                            task["totals"]["total_tokens"])

        with open(self.mod.report_path("ATTR-OVERLAP-1"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("Unknown is not the main session", report)
        self.assertIn("not evidence of a skipped or failed review", report)

    def test_registry_is_session_scoped_idempotent_and_conflicts_are_counted(self):
        task = {"task_id": "ATTR-REGISTRY-1", "totals": self.mod.blank_agg()}
        attribution = self.mod.prepare_attribution_block(task)
        original_events = [
            {"agent_id": "shared-agent", "event": "started", "ts": 100.0,
             "agent_name": "coder", "model": None},
            {"agent_id": "shared-agent", "event": "completed", "ts": 110.0,
             "agent_name": "coder", "model": "model-a"},
            {"agent_id": "started-only", "event": "started", "ts": 120.0,
             "agent_name": "tester", "model": None},
            {"agent_id": "completed-only", "event": "completed", "ts": 130.0,
             "agent_name": "reviewer", "model": "model-b"},
            # Missing/empty source IDs are ignored rather than synthesized.
            {"agent_id": None, "event": "started", "ts": 140.0,
             "agent_name": "guessed-agent", "model": None},
        ]
        delta = {"unknown": {}, "invocation_events": original_events}
        self.mod.merge_attribution_delta(attribution, delta, "session-a")
        self.mod.merge_attribution_delta(attribution, delta, "session-a")

        shared = attribution["observed_invocations"]["session-a"]["shared-agent"]
        self.assertEqual(shared["conflicting_observations"], 0)
        self.assertEqual(len(attribution["observed_invocations"]["session-a"]), 3)

        conflicting_completion = {
            "unknown": {},
            "invocation_events": [{
                "agent_id": "shared-agent",
                "event": "completed",
                "ts": 111.0,
                "agent_name": "reviewer",
                "model": "model-c",
            }],
        }
        self.mod.merge_attribution_delta(
            attribution, conflicting_completion, "session-a"
        )
        self.mod.merge_attribution_delta(
            attribution,
            {"unknown": {}, "invocation_events": [{
                "agent_id": "shared-agent",
                "event": "started",
                "ts": 100.0,
                "agent_name": "coder",
                "model": None,
            }]},
            "session-b",
        )

        summary = self.mod.summarize_attribution(task)
        self.assertEqual(shared["conflicting_observations"], 3)
        self.assertEqual(summary["registry_invocations"], 4)
        self.assertEqual(summary["registry_sessions"], 2)
        self.assertEqual(summary["registry_conflicts"], 3)
        self.assertEqual(summary["registry"]["tester"]["started_only"], 1)
        self.assertEqual(summary["registry"]["reviewer"]["completed_only"], 1)
        self.assertEqual(
            attribution["observed_invocations"]["session-b"]["shared-agent"][
                "conflicting_observations"
            ],
            0,
        )

    def test_legacy_snapshot_and_incremental_calls_reconcile_without_replay(self):
        task_id = "ATTR-LEGACY-1"
        legacy_call = {
            "prompt_tokens": 20, "completion_tokens": 10,
            "reasoning_tokens": 2, "cache_read_tokens": 3,
            "cache_write_tokens": 1, "total_tokens": 30,
            "duration_ms": 500, "effort_key": "unknown",
        }
        legacy = self.mod.blank_agg()
        self.mod.add_agg(legacy, legacy_call)
        totals = dict(legacy)
        totals.update({
            "first_call_ts": self.t0.timestamp() - 60,
            "last_call_ts": self.t0.timestamp() - 1,
            "nano_aiu": 13,
            "premium_requests": 2,
        })
        self.mod.atomic_write(
            self.mod.task_path(task_id),
            json.dumps({
                "task_id": task_id,
                "totals": totals,
                "by_model": {"legacy-model": legacy},
                "by_effort": {"unknown": legacy},
            }),
        )

        session_id = "sess-attribution-legacy"
        t1 = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat current-model", t1, t1 + timedelta(seconds=1),
                      usage_attrs("current-model", prompt=10, completion=5)),
        ])
        self.assertEqual(self.ingest(session_id, task_id), 0)
        first = self.task(task_id)
        att_first = copy.deepcopy(first["attribution"])
        self.assertEqual(att_first["legacy"], legacy)
        reason = self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK
        self.assertEqual(att_first["unknown"][reason]["call_count"], 1)
        self.assertEqual(att_first["unknown"][reason]["total_tokens"], 15)
        self.assertEqual(self.mod.summarize_attribution(first)["legacy_calls"], 1)

        # A no-op replay must not add the same call to either total or
        # attribution buckets.
        self.assertEqual(self.ingest(session_id, task_id), 0)
        replayed = self.task(task_id)
        self.assertEqual(replayed["attribution"], att_first)
        self.assertEqual(replayed["totals"]["call_count"], 2)

        t2 = self.t0 + timedelta(minutes=2)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat current-model", t2, t2 + timedelta(seconds=1),
                      usage_attrs("current-model", prompt=2, completion=3)),
        ])
        self.assertEqual(self.ingest(session_id, task_id), 0)
        final = self.task(task_id)
        summary = self.mod.summarize_attribution(final)
        self.assertEqual(final["attribution"]["legacy"], legacy)
        self.assertEqual(final["attribution"]["unknown"][reason]["call_count"], 2)
        self.assertEqual(final["attribution"]["unknown"][reason]["total_tokens"], 20)
        self.assertEqual(final["totals"]["call_count"], 3)
        self.assertEqual(final["totals"]["total_tokens"], 50)
        self.assertTrue(summary["reconciled"])

    def test_old_delta_without_attribution_falls_back_to_unknown(self):
        task = {"task_id": "ATTR-OLD-DELTA-1"}
        call_agg = self.mod.blank_agg()
        self.mod.add_agg(call_agg, {
            "prompt_tokens": 4, "completion_tokens": 6,
            "reasoning_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 10,
            "duration_ms": 10, "effort_key": "unknown",
        })
        old_delta = {
            "by_model": {"legacy-delta-model": call_agg},
            "by_effort": {},
            "agent_summaries": [],
            "repositories": [],
            "min_ts": None,
            "max_ts": None,
            "nano_aiu_delta": 0,
            "premium_requests_delta": 0,
        }

        self.mod.merge_delta_into_task(task, old_delta, "session-old-delta", task["task_id"])
        summary = self.mod.summarize_attribution(task)
        reason = self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK
        self.assertEqual(summary["unknown"][reason], {"calls": 1, "total_tokens": 10})
        self.assertEqual(summary["attributed_calls"], 0)
        self.assertTrue(summary["reconciled"])

    def test_malformed_attribution_is_preserved_while_new_usage_is_counted(self):
        task_id = "ATTR-MALFORMED-1"
        malformed = {"schema_version": 99, "custom": {"keep": True}}
        self.mod.atomic_write(
            self.mod.task_path(task_id),
            json.dumps({"task_id": task_id, "attribution": malformed}),
        )
        session_id = "sess-attribution-malformed"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat model-x", start, start + timedelta(seconds=1),
                      usage_attrs("model-x", prompt=2, completion=3)),
        ])
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            self.assertEqual(self.ingest(session_id, task_id), 0)

        task = self.task(task_id)
        self.assertEqual(task["attribution"], malformed)
        self.assertEqual(task["totals"]["call_count"], 1)
        self.assertEqual(self.mod.summarize_attribution(task)["status"], "unreadable")
        self.assertIn("unsupported/malformed attribution block", stderr_capture.getvalue())
        with open(self.mod.report_path(task_id), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("unsupported or malformed schema", report)


class TestReviewRecords(BaseTestCase):
    """The explicit review recorder validates bounded per-work-item
    outcomes and never mutates usage/pricing/report data."""

    def review_record(self, **overrides):
        record = {
            "schema": "copilot-task-report.review-record",
            "schema_version": 1,
            "record_id": "work-1-reviewer-r0",
            "task_id": "REVIEW-1",
            "session_id": "unknown",
            "cycle_id": "work-1",
            "stage": "reviewer",
            "round": 0,
            "verdict": "needs-fixes",
            "invocation_id": "unknown",
            "invocation_unknown_reason": "task result did not expose an agent id",
            "provenance": {
                "source": "orchestrator-supplied",
                "basis": "agent-response",
                "reference": "reviewer broad review: findings 1 and 2",
            },
        }
        record.update(overrides)
        return record

    def write_review_input(self, record, filename="review-input.json"):
        path = os.path.join(self.tmp, filename)
        self.mod.atomic_write(path, json.dumps(record))
        return path

    def run_recorder(self, record):
        path = self.write_review_input(record)

        class Args:
            pass

        args = Args()
        args.input = path
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = self.mod.cmd_record_review(args)
        return rc, stdout.getvalue(), stderr.getvalue()

    def apply(self, task, raw_record):
        record = self.mod.validate_review_record(raw_record)
        return self.mod.apply_review_record(task, record)

    def test_review_record_validation_is_strict_and_normalizes_task_id(self):
        valid = self.review_record(task_id="  review-123  ")
        parsed = self.mod.validate_review_record(valid)
        self.assertEqual(parsed["task_id"], "REVIEW-123")
        self.assertEqual(parsed["invocation_id"], "unknown")
        self.assertEqual(parsed["invocation_unknown_reason"],
                         "task result did not expose an agent id")

        invalid_records = []
        bad = copy.deepcopy(valid)
        bad["schema_version"] = True
        invalid_records.append(("boolean schema version", bad))
        bad = copy.deepcopy(valid)
        bad["schema_version"] = 2
        invalid_records.append(("unsupported schema version", bad))
        bad = copy.deepcopy(valid)
        bad["round"] = True
        invalid_records.append(("boolean round", bad))
        bad = copy.deepcopy(valid)
        bad["round"] = 4
        invalid_records.append(("round beyond budget", bad))
        bad = copy.deepcopy(valid)
        bad["unrecognized"] = "field"
        invalid_records.append(("unknown field", bad))
        bad = copy.deepcopy(valid)
        bad.pop("invocation_unknown_reason")
        invalid_records.append(("unknown invocation without reason", bad))
        bad = copy.deepcopy(valid)
        bad["invocation_id"] = "agent-real"
        invalid_records.append(("unknown reason with a real invocation id", bad))
        bad = copy.deepcopy(valid)
        bad["invocation_id"] = "Unknown"
        invalid_records.append(("noncanonical unknown literal", bad))
        bad = copy.deepcopy(valid)
        bad["stage"] = "coder"
        invalid_records.append(("unsupported stage", bad))
        bad = copy.deepcopy(valid)
        bad["task_id"] = "../outside"
        invalid_records.append(("unsafe task id", bad))
        bad = copy.deepcopy(valid)
        bad["provenance"]["source"] = "telemetry"
        invalid_records.append(("unsupported provenance source", bad))
        bad = copy.deepcopy(valid)
        bad["provenance"]["reference"] = "line one\nline two"
        invalid_records.append(("multiline provenance", bad))
        bad = copy.deepcopy(valid)
        bad["provenance"]["reference"] = "x" * 501
        invalid_records.append(("overlong provenance", bad))

        for label, invalid in invalid_records:
            with self.subTest(case=label):
                with self.assertRaises(self.mod.ReviewRecordError):
                    self.mod.validate_review_record(invalid)

    def test_review_input_rejects_duplicate_keys_nonfinite_oversize_and_bad_utf8(self):
        valid_text = json.dumps(self.review_record())
        invalid_inputs = [
            ("empty", b" \n"),
            ("invalid UTF-8", b"\xff"),
            ("duplicate JSON keys", b'{"schema":"one","schema":"two"}'),
            ("NaN", valid_text.replace('"round": 0', '"round": NaN').encode("utf-8")),
            ("oversize", b" " * (self.mod.REVIEW_INPUT_MAX_BYTES + 1)),
        ]
        path = os.path.join(self.tmp, "strict-input.json")
        for label, contents in invalid_inputs:
            with self.subTest(case=label):
                with open(path, "wb") as f:
                    f.write(contents)
                with self.assertRaises(self.mod.ReviewRecordError):
                    self.mod.load_review_record_input(path)

    def test_review_budget_is_per_cycle_and_stage_with_ordered_terminal_rounds(self):
        task = {"task_id": "REVIEW-1"}
        first = self.review_record(verdict="needs-fixes")
        self.assertEqual(self.apply(task, first), "recorded")
        after_first = copy.deepcopy(task)

        # An identical record replay is idempotent; a different session
        # cannot create another broad review in the same work-item cycle.
        self.assertEqual(self.apply(task, first), "duplicate")
        self.assertEqual(task, after_first)
        second_broad = self.review_record(
            record_id="work-1-reviewer-r0-again", session_id="different-session"
        )
        with self.assertRaisesRegex(self.mod.ReviewRecordError, "second one"):
            self.apply(task, second_broad)
        out_of_order = self.review_record(
            record_id="work-1-reviewer-r2", round=2
        )
        with self.assertRaisesRegex(self.mod.ReviewRecordError, "requires round 1"):
            self.apply(task, out_of_order)
        self.assertEqual(task, after_first)

        focused_approval = self.review_record(
            record_id="work-1-reviewer-r1", session_id="different-session",
            round=1, verdict="approved",
        )
        self.assertEqual(self.apply(task, focused_approval), "recorded")
        post_approval = self.review_record(
            record_id="work-1-reviewer-r2", session_id="third-session",
            round=2, verdict="needs-fixes",
        )
        self.assertEqual(self.apply(task, post_approval), "recorded")
        outcome = "\n".join(self.mod.render_review_section(task))
        self.assertIn("latest verdict needs-fixes at focused 2 of 3", outcome)
        self.assertIn("supersedes the earlier approval at focused 1 of 3", outcome)

        # Only escalation ends a stage before the round limit; no later
        # focused round can be recorded after an escalated verdict.
        escalated = self.review_record(
            record_id="work-escalated-reviewer-r0", cycle_id="work-escalated",
            verdict="escalated",
        )
        self.assertEqual(self.apply(task, escalated), "recorded")
        after_escalation = copy.deepcopy(task)
        after_escalation_round = self.review_record(
            record_id="work-escalated-reviewer-r1", cycle_id="work-escalated",
            round=1, verdict="needs-fixes",
        )
        with self.assertRaisesRegex(self.mod.ReviewRecordError, "already ended"):
            self.apply(task, after_escalation_round)
        self.assertEqual(task, after_escalation)

        # The test-reviewer stage has an independent budget, and a genuinely
        # separate work cycle may be recorded on the same task.
        test_review = self.review_record(
            record_id="work-1-test-reviewer-r0", stage="test-reviewer",
            verdict="approved",
        )
        self.assertEqual(self.apply(task, test_review), "recorded")
        next_work_item = self.review_record(
            record_id="work-2-reviewer-r0", cycle_id="work-2",
            verdict="unknown",
        )
        self.assertEqual(self.apply(task, next_work_item), "recorded")

    def test_approved_review_test_review_fixes_then_same_cycle_reviewer_round(self):
        task = {"task_id": "REVIEW-1"}
        broad_approval = self.review_record(
            record_id="mixed-reviewer-r0", verdict="approved",
        )
        test_review_needs_fixes = self.review_record(
            record_id="mixed-test-reviewer-r0", stage="test-reviewer",
            verdict="needs-fixes",
        )
        reviewer_followup = self.review_record(
            record_id="mixed-reviewer-r1", round=1, verdict="needs-fixes",
        )

        self.assertEqual(self.apply(task, broad_approval), "recorded")
        self.assertEqual(self.apply(task, test_review_needs_fixes), "recorded")
        self.assertEqual(self.apply(task, reviewer_followup), "recorded")
        outcome = "\n".join(self.mod.render_review_section(task))
        self.assertIn("supersedes the earlier approval at broad", outcome)

    def test_known_invocation_is_exact_session_lookup_not_call_token_ownership(self):
        task = {"task_id": "REVIEW-ID-1", "totals": self.mod.blank_agg()}
        att = self.mod.prepare_attribution_block(task)
        call = {
            "prompt_tokens": 8, "completion_tokens": 2,
            "reasoning_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 10,
            "duration_ms": 100, "effort_key": "unknown",
        }
        self.mod.add_agg(task["totals"], call)
        unknown = att["unknown"].setdefault(
            self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK, self.mod.blank_agg()
        )
        self.mod.add_agg(unknown, call)
        record = self.review_record(
            record_id="review-known-id",
            task_id="REVIEW-ID-1",
            session_id="session-exact",
            invocation_id="agent-observed-later",
        )
        record.pop("invocation_unknown_reason")
        self.assertEqual(self.apply(task, record), "recorded")

        pending_markdown = "\n".join(self.mod.render_review_section(task))
        self.assertIn("not observed in this task's registry", pending_markdown)
        self.assertEqual(
            self.mod.observed_invocation_status(
                task, "other-session", "agent-observed-later"
            ),
            "not observed in this task's registry (not ingested yet, or recorded under another task)",
        )
        self.assertEqual(self.mod.summarize_attribution(task)["attributed_calls"], 0)

        self.mod.merge_attribution_delta(
            att,
            {"unknown": {}, "invocation_events": [{
                "agent_id": "agent-observed-later",
                "event": "started",
                "ts": 123.0,
                "agent_name": "reviewer",
                "model": None,
            }]},
            "session-exact",
        )
        observed_markdown = "\n".join(self.mod.render_review_section(task))
        summary = self.mod.summarize_attribution(task)
        self.assertIn("observed in events.jsonl registry", observed_markdown)
        self.assertEqual(summary["unknown_calls"], 1)
        self.assertEqual(summary["attributed_calls"], 0)
        self.assertTrue(summary["reconciled"])

    def test_recording_preserves_usage_pricing_and_existing_report(self):
        task_id = "PRESERVE-1"
        task = {
            "task_id": task_id,
            "totals": dict(self.mod.blank_agg(), call_count=2, total_tokens=15),
            "by_model": {"model-before-review": dict(self.mod.blank_agg(),
                                                      call_count=2, total_tokens=15)},
            "official_cost": {"sentinel": {"calls": 2, "usd": "frozen"}},
            "attribution": {"schema_version": 1, "legacy": {"call_count": 2},
                            "unknown": {}, "observed_invocations": {},
                            "provenance": {"sentinel": "preserved"}},
        }
        before = copy.deepcopy(task)
        self.mod.atomic_write(self.mod.task_path(task_id), json.dumps(task))
        report_path = self.mod.report_path(task_id)
        self.mod.atomic_write(report_path, "# previously generated report\n")
        with open(report_path, encoding="utf-8") as f:
            report_before = f.read()

        record = self.review_record(task_id=task_id, verdict="approved")
        with mock.patch.object(
            self.mod, "build_delta", side_effect=AssertionError("must not ingest")
        ), mock.patch.object(
            self.mod, "refresh_official_pricing_if_due",
            side_effect=AssertionError("must not fetch pricing")
        ), mock.patch.object(
            self.mod, "render_markdown",
            side_effect=AssertionError("must not regenerate report")
        ):
            rc, _, stderr = self.run_recorder(record)

        self.assertEqual(rc, 0, stderr)
        stored = self.task(task_id)
        for key, value in before.items():
            self.assertEqual(stored[key], value, key)
        self.assertIn("work-1-reviewer-r0", stored["reviews"]["records"])
        with open(report_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), report_before)

    def test_new_task_cli_record_is_minimal_idempotent_and_does_not_render(self):
        home = os.path.join(self.tmp, "cli-home")
        reports = os.path.join(self.tmp, "cli-reports")
        os.makedirs(home)
        record = self.review_record(
            record_id="cli-cycle-review-r0",
            task_id="cli-9",
            cycle_id="cli-cycle",
            verdict="approved",
        )
        input_path = self.write_review_input(record, "cli-review.json")
        env = os.environ.copy()
        env["HOME"] = home
        env["COPILOT_TASK_REPORTS_DIR"] = reports
        env["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"

        command = [
            sys.executable, MODULE_PATH, "record-review",
            "--input", input_path,
        ]
        first = subprocess.run(
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        task_path = os.path.join(
            home, ".copilot", "task-reports", "tasks", "CLI-9.json"
        )
        with open(task_path, encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(set(stored), {"task_id", "reviews"})
        self.assertEqual(stored["task_id"], "CLI-9")
        self.assertFalse(os.path.exists(os.path.join(reports, "CLI-9.md")))
        self.assertIn("not regenerated", first.stdout)

        with open(task_path, "rb") as f:
            before_replay = f.read()
        replay = subprocess.run(
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertIn("nothing changed", replay.stdout)
        with open(task_path, "rb") as f:
            self.assertEqual(f.read(), before_replay)

    def test_unassigned_review_stays_unassigned_after_later_named_ingest(self):
        record = self.review_record(
            record_id="unassigned-review-r0",
            task_id="UNASSIGNED",
            cycle_id="unassigned-work",
            verdict="approved",
        )
        rc, _, stderr = self.run_recorder(record)
        self.assertEqual(rc, 0, stderr)

        session_id = "sess-review-later-assigned"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat model-after-review", start,
                      start + timedelta(seconds=1),
                      usage_attrs("model-after-review", prompt=4, completion=2)),
        ])
        self.assertEqual(self.ingest(session_id, "LATER-ASSIGNED-1"), 0)
        unassigned = self.task("UNASSIGNED")
        assigned = self.task("LATER-ASSIGNED-1")
        self.assertIn("unassigned-review-r0", unassigned["reviews"]["records"])
        self.assertNotIn("reviews", assigned)
        self.assertEqual(assigned["attribution"]["legacy"]["call_count"], 0)
        self.assertEqual(
            assigned["attribution"]["unknown"][
                self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK
            ]["call_count"],
            1,
        )

    def test_corrupt_existing_task_file_is_never_overwritten(self):
        task_id = "CORRUPT-1"
        path = self.mod.task_path(task_id)
        original = b'{"task_id": "CORRUPT-1",'
        with open(path, "wb") as f:
            f.write(original)

        rc, _, stderr = self.run_recorder(self.review_record(task_id=task_id))
        self.assertEqual(rc, 1)
        self.assertIn("refusing to overwrite it", stderr)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original)

    def test_ingest_rejects_invalid_existing_task_without_consuming_telemetry(self):
        task_id = "STRICT-1"
        session_id = "sess-strict-task-file"
        task_path = self.mod.task_path(task_id)
        invalid_documents = [
            ("malformed JSON", b'{"task_id":'),
            ("non-object JSON", b"[]"),
            ("invalid UTF-8", b"\xff"),
            ("mismatched task id", b'{"task_id":"OTHER-1"}'),
            ("null task id", b'{"task_id":null}'),
        ]
        original_state = b'{"keep":{"task_id":"KEEP-1","sentinel":true}}\n'
        with open(self.mod.STATE_FILE, "wb") as f:
            f.write(original_state)

        t = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat model-strict", t, t + timedelta(seconds=1),
                      usage_attrs("model-strict", prompt=7, completion=3)),
        ])
        write_jsonl(self.events_path(session_id), [
            {"type": "session.usage_checkpoint", "timestamp": iso(t),
             "data": {"totalNanoAiu": 1_000_000_000, "totalPremiumRequests": 2}},
        ])

        for label, contents in invalid_documents:
            with self.subTest(case=label):
                with open(task_path, "wb") as f:
                    f.write(contents)
                with mock.patch.object(
                    self.mod, "build_delta",
                    side_effect=AssertionError("invalid task file must be checked before telemetry"),
                ) as build_delta:
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        rc = self.ingest(session_id, task_id)

                self.assertEqual(rc, 1)
                self.assertIn("was not ingested", stderr.getvalue())
                build_delta.assert_not_called()
                with open(task_path, "rb") as f:
                    self.assertEqual(f.read(), contents)
                with open(self.mod.STATE_FILE, "rb") as f:
                    self.assertEqual(f.read(), original_state)

        # Repairing the destination lets the still-unconsumed telemetry be
        # ingested on a later attempt.
        self.mod.atomic_write(task_path, json.dumps({"task_id": task_id}))
        self.assertEqual(self.ingest(session_id, task_id), 0)
        repaired = self.task(task_id)
        self.assertEqual(repaired["totals"]["call_count"], 1)
        self.assertEqual(repaired["totals"]["nano_aiu"], 1_000_000_000)
        self.assertEqual(repaired["totals"]["premium_requests"], 2)

    def test_record_review_accepts_normalized_and_legacy_task_identity(self):
        cases = [
            ("LEGACY-17", {"jira_key": "legacy-17"}, " legacy-17 "),
            ("LEGACY-18", {"task_id": " legacy-18 "}, "LEGACY-18"),
        ]
        for index, (task_id, existing, supplied_id) in enumerate(cases):
            with self.subTest(task_id=task_id):
                path = self.mod.task_path(task_id)
                self.mod.atomic_write(path, json.dumps(existing))
                record = self.review_record(
                    task_id=supplied_id,
                    record_id="legacy-record-%d" % index,
                    cycle_id="legacy-cycle-%d" % index,
                )
                with mock.patch.object(
                    self.mod, "refresh_official_pricing_if_due",
                    side_effect=AssertionError("record-review must not refresh or reprice"),
                ) as refresh:
                    rc, _, stderr = self.run_recorder(record)

                self.assertEqual(rc, 0, stderr)
                refresh.assert_not_called()
                stored = self.task(task_id)
                for key, value in existing.items():
                    self.assertEqual(stored[key], value)
                self.assertIn(record["record_id"], stored["reviews"]["records"])

    def test_review_markdown_escapes_provenance_and_unknown_is_not_success(self):
        task = {"task_id": "REVIEW-MD-1"}
        record = self.review_record(
            record_id="review-md-r0",
            task_id="REVIEW-MD-1",
            verdict="unknown",
            provenance={
                "source": "orchestrator-supplied",
                "basis": "user-supplied",
                "reference": "review | outcome unclear",
            },
        )
        self.assertEqual(self.apply(task, record), "recorded")
        markdown = "\n".join(self.mod.render_review_section(task))
        self.assertIn("review \\| outcome unclear", markdown)
        self.assertIn("explicitly unknown", markdown)
        self.assertIn("unknown outcome at latest recorded round", markdown)

    def test_review_markdown_escapes_backslash_before_pipe_in_table_cell(self):
        task = {"task_id": "REVIEW-MD-BACKSLASH-1"}
        reference = r"literal a\|b"
        record = self.review_record(
            record_id="review-md-backslash-r0",
            task_id=task["task_id"],
            verdict="unknown",
            provenance={
                "source": "orchestrator-supplied",
                "basis": "user-supplied",
                "reference": reference,
            },
        )
        self.assertEqual(self.apply(task, record), "recorded")

        markdown = "\n".join(self.mod.render_review_section(task))
        expected = "literal a" + "\\" * 3 + "|b"
        self.assertIn(expected, markdown)

    def test_recorded_review_survives_ingest_and_explicit_report(self):
        task_id = "REVIEW-INTEGRATION-42"
        record = self.review_record(
            record_id="integration-review-r0",
            task_id=task_id,
            cycle_id="integration-cycle",
        )
        rc, _, stderr = self.run_recorder(record)
        self.assertEqual(rc, 0, stderr)
        reviews_before_ingest = copy.deepcopy(self.task(task_id)["reviews"])

        session_id = "sess-review-integration"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat model-review-integration", start,
                      start + timedelta(seconds=1),
                      usage_attrs("model-review-integration", prompt=4, completion=2)),
        ])
        self.assertEqual(self.ingest(session_id, task_id), 0)

        stored = self.task(task_id)
        self.assertEqual(stored["reviews"], reviews_before_ingest)
        self.assertEqual(stored["attribution"]["legacy"]["call_count"], 0)
        reason = self.mod.ATTRIBUTION_REASON_NO_SUPPORTED_LINK
        self.assertEqual(stored["attribution"]["unknown"][reason]["call_count"], 1)
        self.assertEqual(stored["totals"]["call_count"], 1)

        # Recording and ingesting are not a substitute for the explicit
        # report command; exercise that command with a normalizable ID.
        class Args:
            pass

        args = Args()
        args.task_id = " review-integration-42 "
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = self.mod.cmd_report(args)
        self.assertEqual(rc, 0, stderr.getvalue())
        with open(self.mod.report_path(task_id), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("Recorded Reviews", report)
        self.assertIn("integration-cycle", report)
        self.assertIn("Recorded Reviews", stdout.getvalue())
        self.assertIn("integration-cycle", stdout.getvalue())

    def test_conflicting_record_id_is_rejected_without_changing_task_bytes(self):
        record = self.review_record(
            record_id="conflicting-review-r0",
            task_id="REVIEW-CONFLICT-42",
            cycle_id="conflicting-cycle",
        )
        rc, _, stderr = self.run_recorder(record)
        self.assertEqual(rc, 0, stderr)

        task_path = self.mod.task_path(record["task_id"])
        with open(task_path, "rb") as f:
            before = f.read()
        conflicting = copy.deepcopy(record)
        conflicting["verdict"] = "approved"
        rc, _, stderr = self.run_recorder(conflicting)

        self.assertEqual(rc, 1)
        self.assertIn("different content", stderr)
        with open(task_path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_malformed_existing_reviews_block_is_rejected_without_overwrite(self):
        task_id = "REVIEW-MALFORMED-42"
        task_path = self.mod.task_path(task_id)
        malformed_task = {
            "task_id": task_id,
            "reviews": {
                "schema_version": self.mod.REVIEWS_SCHEMA_VERSION,
                "records": [],
            },
        }
        original = json.dumps(malformed_task, separators=(",", ":")).encode("utf-8")
        with open(task_path, "wb") as f:
            f.write(original)

        rc, _, stderr = self.run_recorder(self.review_record(task_id=task_id))

        self.assertEqual(rc, 1)
        self.assertIn("unsupported or malformed 'reviews' block", stderr)
        with open(task_path, "rb") as f:
            self.assertEqual(f.read(), original)


class TestReportRendering(BaseTestCase):
    """cmd_ingest must write a Markdown report with meaningful rendered
    values, and cmd_report must print that report (not just return 0)."""

    def _seed_report_data(self, session_id, task):
        self.write_official_snapshot()
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
        self.assertIn("$0.0100", md)  # official fixture: (800*3 + 200*0.3 + 500*15)/1e6
        self.assertIn("Estimated USD cost (official GitHub per-token rates recorded at ingestion", md)
        self.assertIn("Official rate source", md)
        self.assertIn("Estimated Cost Coverage", md)
        self.assertNotIn("Total company fixed charge", md)
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
        self.assertNotIn("Total company fixed charge", md)
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


class TestOfficialPricingParsing(BaseTestCase):
    def test_provider_headers_model_ids_rates_and_thresholds(self):
        snapshot = self.official_snapshot(fetched_epoch=10_000)
        self.assertEqual(
            snapshot["providers"],
            ["OpenAI", "Anthropic", "Google", "DeepSeek", "Moonshot", "xAI", "Meta", "Mistral AI"],
        )
        self.assertEqual(
            set(snapshot["models"]),
            {
                "gpt-6-luna", "gpt-6.1-sol", "claude-opus-4", "claude-sonnet-5",
                "gemini-3.8-flash", "deepseek-v4", "kimi-k3", "grok-4.7",
                "llama-4", "mistral-large",
            },
        )

        luna = snapshot["models"]["gpt-6-luna"]["tiers"]["default"]
        self.assertEqual(
            (luna["input"], luna["cached_input"], luna["cache_write"], luna["output"]),
            (0.1, 0.01, 0.125, 0.5),
        )
        sol = snapshot["models"]["gpt-6.1-sol"]
        self.assertEqual(sol["threshold"], {
            "label": "272K",
            "tokens": 272_000,
            "ambiguous_upper_tokens": 272 * 1024,
        })
        self.assertEqual(
            tuple(sol["tiers"]["default"][k] for k in ("input", "cached_input", "cache_write", "output")),
            (2.0, 0.1, 2.5, 10.0),
        )
        self.assertEqual(
            tuple(sol["tiers"]["long_context"][k] for k in ("input", "cached_input", "cache_write", "output")),
            (4.0, 0.2, 5.0, 15.0),
        )
        opus = snapshot["models"]["claude-opus-4"]
        self.assertEqual(
            tuple(opus["tiers"]["default"][k] for k in ("input", "cached_input", "cache_write", "output")),
            (4.0, 0.2, None, 20.0),
        )
        self.assertEqual(opus["tiers"]["default"]["cache_write_status"], self.mod.CACHE_WRITE_NOT_APPLICABLE)
        self.assertEqual(
            tuple(opus["tiers"]["long_context"][k] for k in ("input", "cached_input", "cache_write", "output")),
            (4.0, 0.2, None, 15.0),
        )
        self.assertEqual(
            snapshot["models"]["gemini-3.8-flash"]["tiers"]["default"]["cache_write_status"],
            self.mod.CACHE_WRITE_NOT_LISTED,
        )
        self.assertEqual(snapshot["models"]["gemini-3.8-flash"]["provider"], "Google")

    def test_malformed_markdown_rejects_entire_snapshot(self):
        malformed = [
            OFFICIAL_PRICING_MARKDOWN.replace("## Pricing tables", "## Prices"),
            OFFICIAL_PRICING_MARKDOWN.replace("$2 | $0.1 | $2.5 | $10", "$bad | $0.1 | $2.5 | $10"),
            OFFICIAL_PRICING_MARKDOWN.replace("≤ 272K", "< 272K"),
        ]
        for page in malformed:
            with self.subTest(page=page[:40]):
                with self.assertRaises(self.mod.OfficialPricingError):
                    self.mod.build_official_snapshot(page.encode("utf-8"), 10_000)

    def test_snapshot_validation_rejects_bool_negative_and_nonfinite_rates(self):
        valid = self.official_snapshot(fetched_epoch=10_000)
        for invalid in (True, -0.01, float("inf"), float("nan")):
            with self.subTest(rate=invalid):
                candidate = copy.deepcopy(valid)
                candidate["models"]["gpt-6-luna"]["tiers"]["default"]["input"] = invalid
                with self.assertRaises(self.mod.OfficialPricingError):
                    self.mod.validate_official_snapshot(candidate)

    def test_snapshot_metadata_provider_hash_and_lookup_are_validated(self):
        valid = self.official_snapshot(fetched_epoch=10_000)
        corruptions = [
            ("providers is not a list", lambda snap: snap.update(providers="OpenAI")),
            ("provider list contains an empty name", lambda snap: snap["providers"].__setitem__(0, "")),
            ("model provider is empty", lambda snap: snap["models"]["gpt-6-luna"].update(provider="")),
            ("fetched timestamp disagrees with epoch", lambda snap: snap.update(fetched_at="1970-01-01T00:00:00Z")),
            ("content hash is malformed", lambda snap: snap.update(content_sha256="not-a-sha256")),
            ("snapshot id disagrees with hash", lambda snap: snap.update(snapshot_id="sha256:" + "0" * 16)),
            ("lookup disagrees with models", lambda snap: snap["lookup"].update({"gpt-6-luna": "not-a-model"})),
        ]
        for label, corrupt in corruptions:
            with self.subTest(corruption=label):
                candidate = copy.deepcopy(valid)
                corrupt(candidate)
                with self.assertRaises(self.mod.OfficialPricingError):
                    self.mod.validate_official_snapshot(candidate)

    def test_malformed_notes_reject_cache_but_null_notes_are_safe(self):
        valid = self.official_snapshot(fetched_epoch=time.time())
        for malformed_notes in ("not-a-list", ["valid note", 7], {"note": "unexpected"}):
            with self.subTest(notes=malformed_notes):
                candidate = copy.deepcopy(valid)
                candidate["models"]["gpt-6-luna"]["notes"] = malformed_notes
                self.mod.atomic_write(
                    self.mod.official_pricing_cache_path(),
                    json.dumps(candidate),
                )
                cached, error = self.mod.read_official_snapshot_cache()
                self.assertIsNone(cached)
                self.assertIn("'notes' must be a list of strings", error)

        null_notes = copy.deepcopy(valid)
        null_notes["models"]["gpt-6-luna"]["notes"] = None
        self.mod.atomic_write(
            self.mod.official_pricing_cache_path(),
            json.dumps(null_notes),
        )
        cached, error = self.mod.read_official_snapshot_cache()
        self.assertIsNone(error)
        self.assertEqual(cached["models"]["gpt-6-luna"]["notes"], None)

        call = {
            "model": "gpt-6-luna",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "usage_present": {"input": True, "output": True},
        }
        delta = self.mod.build_official_cost_delta(
            [call],
            {"snapshot": cached, "status": "fresh", "age_seconds": 1},
        )
        self.assertEqual(delta["by_model"]["gpt-6-luna"]["priced"][cached["snapshot_id"]]["calls"], 1)
        self.assertNotIn("gpt-6-luna", delta["snapshot"]["notes"])

    def test_cache_read_write_output_and_reasoning_are_priced_once(self):
        snapshot = self.official_snapshot(fetched_epoch=10_000)
        call = {
            "model": "gpt-6-luna",
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "reasoning_tokens": 100,
            "cache_read_tokens": 300,
            "cache_write_tokens": 200,
            "usage_present": {"input": True, "output": True},
        }
        result, reason = self.mod.price_call_official(call, snapshot)
        self.assertIsNone(reason)
        self.assertEqual(result["components"], {
            "input_usd": 500 * 0.1 / 1_000_000,
            "cached_input_usd": 300 * 0.01 / 1_000_000,
            "cache_write_usd": 200 * 0.125 / 1_000_000,
            "output_usd": 200 * 0.5 / 1_000_000,
        })
        self.assertAlmostEqual(result["usd"], 178 / 1_000_000)

        # Official "Not applicable" means cache-write tokens are charged at
        # the ordinary input rate. A missing cache-write column is different:
        # a positive cache-write count cannot be priced exactly.
        opus_call = dict(call, model="claude-opus-4", prompt_tokens=1000,
                         completion_tokens=0, reasoning_tokens=0,
                         cache_read_tokens=0, cache_write_tokens=100)
        opus, reason = self.mod.price_call_official(opus_call, snapshot)
        self.assertIsNone(reason)
        self.assertAlmostEqual(opus["components"]["cache_write_usd"], 100 * 4 / 1_000_000)
        unlisted_call = dict(opus_call, model="gemini-3.8-flash")
        unlisted, reason = self.mod.price_call_official(unlisted_call, snapshot)
        self.assertIsNone(unlisted)
        self.assertEqual(reason, self.mod.UNPRICED_CACHE_WRITE_NOT_LISTED)

    def test_tier_threshold_ambiguous_band_and_exact_edges(self):
        snapshot = self.official_snapshot(fetched_epoch=10_000)
        upper = 272 * 1024
        expected = [
            (272_000, self.mod.TIER_DEFAULT, None),
            (272_001, None, self.mod.UNPRICED_TIER_AMBIGUOUS),
            (upper, None, self.mod.UNPRICED_TIER_AMBIGUOUS),
            (upper + 1, self.mod.TIER_LONG, None),
        ]
        for prompt, tier, reason_expected in expected:
            with self.subTest(prompt=prompt):
                result, reason = self.mod.price_call_official({
                    "model": "gpt-6.1-sol",
                    "prompt_tokens": prompt,
                    "completion_tokens": 1,
                    "usage_present": {"input": True, "output": True},
                }, snapshot)
                self.assertEqual(reason, reason_expected)
                self.assertEqual(result["tier"] if result else None, tier)

    def test_unknown_missing_and_inconsistent_usage_are_explicitly_unpriced(self):
        snapshot = self.official_snapshot(fetched_epoch=10_000)
        good = {
            "model": "gpt-6-luna", "prompt_tokens": 10, "completion_tokens": 5,
            "reasoning_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "usage_present": {"input": True, "output": True},
        }
        cases = [
            (None, self.mod.UNPRICED_NO_SNAPSHOT),
            (dict(good, model="auto"), self.mod.UNPRICED_UNKNOWN_MODEL),
            (dict(good, usage_present={"input": False, "output": True}), self.mod.UNPRICED_NO_INPUT),
            (dict(good, usage_present={"input": True, "output": False}), self.mod.UNPRICED_NO_OUTPUT),
            (dict(good, prompt_tokens=-1), self.mod.UNPRICED_NEGATIVE),
            (dict(good, cache_read_tokens=11), self.mod.UNPRICED_CACHE_EXCEEDS_INPUT),
            (dict(good, reasoning_tokens=6), self.mod.UNPRICED_REASONING_EXCEEDS_OUTPUT),
        ]
        for call, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                result, reason = self.mod.price_call_official(call, snapshot if call is not None else None)
                self.assertIsNone(result)
                self.assertEqual(reason, expected_reason)


class TestOfficialPricingTlsFallback(unittest.TestCase):
    """The system-curl path is only a verified fallback for Python CA-bundle
    failures. All transport behavior is mocked; these tests never use a
    network connection."""

    def setUp(self):
        self.mod = load_module()

    def test_only_raw_or_wrapped_certificate_verification_errors_fallback(self):
        url = self.mod.OFFICIAL_PRICING_API_URL
        timeout = 7
        max_bytes = 4096
        failures = [
            ssl.SSLCertVerificationError("certificate verify failed"),
            urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed")),
        ]

        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                with mock.patch.object(
                    self.mod, "_default_pricing_fetch_inner", side_effect=failure
                ), mock.patch.object(
                    self.mod, "_curl_pricing_fetch", return_value=b"curl body"
                ) as curl_fetch, contextlib.redirect_stderr(io.StringIO()):
                    result = self.mod._default_pricing_fetch(url, timeout, max_bytes)

                self.assertEqual(result, b"curl body")
                curl_fetch.assert_called_once()
                args = curl_fetch.call_args.args
                self.assertEqual(args[:3], (url, timeout, max_bytes))
                self.assertIn("certificate verify failed", args[3])

    def test_non_certificate_errors_never_fallback(self):
        url = self.mod.OFFICIAL_PRICING_API_URL
        failures = [
            urllib.error.URLError(OSError("connection refused")),
            TimeoutError("request timed out"),
            ssl.SSLError("TLS handshake failed"),
            urllib.error.HTTPError(url, 503, "Unavailable", {}, io.BytesIO(b"")),
        ]

        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                with mock.patch.object(
                    self.mod, "_default_pricing_fetch_inner", side_effect=failure
                ), mock.patch.object(self.mod, "_curl_pricing_fetch") as curl_fetch:
                    with self.assertRaises(type(failure)):
                        self.mod._default_pricing_fetch(url, 7, 4096)
                curl_fetch.assert_not_called()

    class FakeCurlProcess:
        def __init__(self, output, returncode=0):
            self.stdout = io.BytesIO(output)
            self.returncode = returncode
            self.wait_timeout = None

        def wait(self, timeout=None):
            self.wait_timeout = timeout
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            raise AssertionError("unexpected curl process kill")

    def _curl_output(self, body, status="200", effective_url=None):
        url = self.mod.OFFICIAL_PRICING_API_URL
        if effective_url is None:
            effective_url = url
        trailer = "%s%s %s" % (self.mod._CURL_STATUS_MARKER, status, effective_url)
        return body + trailer.encode("ascii")

    def test_system_curl_uses_verified_https_without_shell_or_redirect_options(self):
        url = self.mod.OFFICIAL_PRICING_API_URL
        body = b"mocked official pricing"
        process = self.FakeCurlProcess(self._curl_output(body))

        with mock.patch.object(self.mod, "_find_system_curl", return_value="/usr/bin/curl"), \
             mock.patch("subprocess.Popen", return_value=process) as popen:
            result = self.mod._curl_pricing_fetch(url, 7, 4096, "certificate error")

        self.assertEqual(result, body)
        argv = popen.call_args.args[0]
        options = argv[1:]
        kwargs = popen.call_args.kwargs
        self.assertEqual(options[0], "-q")
        self.assertIn("--proto", options)
        self.assertEqual(options[options.index("--proto") + 1], "=https")
        self.assertIn("--proto-redir", options)
        self.assertEqual(options[options.index("--proto-redir") + 1], "=https")
        self.assertFalse({"-k", "--insecure", "-L", "--location", "--location-trusted"} & set(options))
        self.assertIs(kwargs["shell"], False)

    def test_system_curl_rejects_bad_status_redirect_oversize_and_missing_trailer(self):
        url = self.mod.OFFICIAL_PRICING_API_URL
        cases = [
            ("non-200", self._curl_output(b"body", status="503"), 4096, "unexpected HTTP status"),
            (
                "effective URL mismatch",
                self._curl_output(b"body", effective_url="https://docs.github.com/other"),
                4096,
                "unexpected final URL",
            ),
            ("oversized body", self._curl_output(b"12345"), 4, "response too large"),
            ("missing status trailer", b"body without trailer", 4096, "missing HTTP status trailer"),
        ]

        for label, output, max_bytes, expected_error in cases:
            with self.subTest(case=label):
                process = self.FakeCurlProcess(output)
                with mock.patch.object(
                    self.mod, "_find_system_curl", return_value="/usr/bin/curl"
                ), mock.patch("subprocess.Popen", return_value=process):
                    with self.assertRaisesRegex(self.mod.OfficialPricingError, expected_error):
                        self.mod._curl_pricing_fetch(url, 7, max_bytes, "certificate error")

    def test_missing_system_curl_is_a_bounded_error(self):
        with mock.patch.object(self.mod, "_find_system_curl", return_value=None), \
             mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(self.mod.OfficialPricingError, "no system curl was found"):
                self.mod._curl_pricing_fetch(
                    self.mod.OFFICIAL_PRICING_API_URL, 7, 4096, "certificate error"
                )
        popen.assert_not_called()


class TestOfficialPricingRefresh(BaseTestCase):
    def test_fresh_cache_skips_fetch_and_expired_cache_refreshes_from_injected_transport(self):
        fetched_epoch = 50_000
        snapshot = self.write_official_snapshot(fetched_epoch=fetched_epoch)
        calls = []
        self.use_fake_pricing_fetcher(
            lambda url, timeout, max_bytes: calls.append((url, timeout, max_bytes))
            or OFFICIAL_PRICING_MARKDOWN.encode("utf-8")
        )

        fresh = self.mod.refresh_official_pricing_if_due(
            now=fetched_epoch + self.mod.OFFICIAL_PRICING_TTL_SECONDS - 1,
        )
        self.assertEqual(fresh["status"], "fresh")
        self.assertFalse(fresh["attempted"])
        self.assertEqual(calls, [])

        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
        expired_at = fetched_epoch + self.mod.OFFICIAL_PRICING_TTL_SECONDS
        refreshed = self.mod.refresh_official_pricing_if_due(now=expired_at)
        self.assertEqual(refreshed["status"], "refreshed")
        self.assertTrue(refreshed["attempted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (
            self.mod.OFFICIAL_PRICING_API_URL,
            self.mod.OFFICIAL_PRICING_TIMEOUT_SECONDS,
            self.mod.OFFICIAL_PRICING_MAX_BYTES,
        ))
        cached, error = self.mod.read_official_snapshot_cache()
        self.assertIsNone(error)
        # Snapshot identity is content-addressed: the fixture contains the
        # same official document, so a refresh updates fetched_at without
        # manufacturing a new content id.
        self.assertEqual(snapshot["snapshot_id"], cached["snapshot_id"])
        self.assertEqual(cached["fetched_epoch"], float(expired_at))

    def test_failed_refresh_keeps_last_good_cache_marks_stale_and_suppresses_retry(self):
        fetched_epoch = 80_000
        snapshot = self.write_official_snapshot(fetched_epoch=fetched_epoch)
        cache_path = self.mod.official_pricing_cache_path()
        with open(cache_path, "rb") as stream:
            before = stream.read()

        failed_at = fetched_epoch + self.mod.OFFICIAL_PRICING_TTL_SECONDS + 10
        self.use_fake_pricing_fetcher(OSError("offline fixture"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.mod.refresh_official_pricing_if_due(now=failed_at)
        self.assertEqual(result["status"], "failed")
        self.assertIn("offline fixture", stderr.getvalue())
        self.assertIn("last valid snapshot", stderr.getvalue())
        with open(cache_path, "rb") as stream:
            self.assertEqual(stream.read(), before)
        ctx = self.mod.load_official_pricing_context(now=failed_at)
        self.assertEqual(ctx["status"], "stale")
        self.assertEqual(ctx["snapshot"]["snapshot_id"], snapshot["snapshot_id"])

        retry_calls = []
        self.mod.PRICING_FETCHER = lambda *_args: retry_calls.append(True) or OFFICIAL_PRICING_MARKDOWN.encode()
        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
        suppressed = self.mod.refresh_official_pricing_if_due(
            now=failed_at + self.mod.OFFICIAL_PRICING_RETRY_BACKOFF_SECONDS - 1,
        )
        self.assertEqual(suppressed["status"], "suppressed")
        self.assertFalse(suppressed["attempted"])
        self.assertEqual(retry_calls, [])

    def test_first_fetch_failure_and_malformed_cache_remain_unpriced_without_replacement(self):
        self.use_fake_pricing_fetcher(OSError("offline fixture"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.mod.refresh_official_pricing_if_due(now=90_000)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no cached snapshot exists", stderr.getvalue())
        self.assertIn("not $0", stderr.getvalue())
        self.assertEqual(self.mod.load_official_pricing_context(now=90_000)["status"], "unavailable")
        self.assertFalse(os.path.exists(self.mod.official_pricing_cache_path()))

        # An invalid pre-existing cache is never silently overwritten by a
        # failed Markdown parse; a valid refresh is required to replace it.
        malformed_cache = b'{"models": [not-json]}'
        self.mod.atomic_write(self.mod.official_pricing_cache_path(), malformed_cache.decode())
        self.use_fake_pricing_fetcher(b"not a pricing article")
        self.mod._OFFICIAL_REFRESH_RESULTS.clear()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = self.mod.refresh_official_pricing_if_due(now=100_000)
        self.assertEqual(result["status"], "failed")
        with open(self.mod.official_pricing_cache_path(), "rb") as stream:
            self.assertEqual(stream.read(), malformed_cache)
        ctx = self.mod.load_official_pricing_context(now=100_000)
        self.assertEqual(ctx["status"], "unavailable")
        self.assertTrue(ctx["cache_error"])

    def test_corrupt_refresh_state_never_breaks_ingest_or_replaces_pricing_cache(self):
        snapshot = self.write_official_snapshot(fetched_epoch=time.time() - 60)
        state_path = self.mod.official_pricing_state_path()
        invalid_states = [
            ("malformed JSON", b'{"last_success_epoch":'),
            ("invalid UTF-8", b"\xff\xfe"),
            ("bad last success", json.dumps({"last_success_epoch": "recent"}).encode("utf-8")),
            ("bad consecutive failures", json.dumps({"consecutive_failures": True}).encode("utf-8")),
        ]
        fetch_calls = []
        self.use_fake_pricing_fetcher(
            lambda *_args: fetch_calls.append(True) or OFFICIAL_PRICING_MARKDOWN.encode("utf-8")
        )

        for index, (label, raw_state) in enumerate(invalid_states):
            with self.subTest(state=label):
                self.mod._OFFICIAL_REFRESH_RESULTS.clear()
                with open(state_path, "wb") as stream:
                    stream.write(raw_state)

                session_id = "sess-corrupt-pricing-state-%d" % index
                start = self.t0 + timedelta(minutes=index + 1)
                write_jsonl(self.otel_path(), [
                    otel_span(
                        session_id,
                        "chat gpt-6-luna",
                        start,
                        start + timedelta(seconds=1),
                        usage_attrs("gpt-6-luna", prompt=100, completion=50),
                    ),
                ])
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(self.ingest(session_id, "CORRUPT-STATE-%d" % index), 0)

                self.assertIn("retry metadata is ignored and reset", stderr.getvalue())
                with open(state_path, encoding="utf-8") as stream:
                    self.assertEqual(json.load(stream), {})
                task = self.task("CORRUPT-STATE-%d" % index)
                self.assertEqual(task["official_cost"]["by_model"]["gpt-6-luna"]["priced"][snapshot["snapshot_id"]]["calls"], 1)
                self.assertEqual(task["totals"]["call_count"], 1)

        self.assertEqual(fetch_calls, [], "a fresh validated cache must not be replaced")
        cached, error = self.mod.read_official_snapshot_cache()
        self.assertIsNone(error)
        self.assertEqual(cached["snapshot_id"], snapshot["snapshot_id"])


class TestOfficialCostIngestion(BaseTestCase):
    def test_stale_snapshot_is_priced_until_seven_days_then_unpriced_without_losing_cost(self):
        now = time.time()
        max_age = self.mod.OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS
        snapshot = self.write_official_snapshot(fetched_epoch=now - max_age + 3600)
        session_id = "sess-stale-official-pricing"

        def span_at(minute):
            start = self.t0 + timedelta(minutes=minute)
            return otel_span(
                session_id,
                "chat gpt-6-luna",
                start,
                start + timedelta(seconds=1),
                usage_attrs("gpt-6-luna", prompt=1000, completion=200),
            )

        self.assertEqual(self.mod.load_official_pricing_context()["status"], "stale")
        write_jsonl(self.otel_path(), [span_at(1)])
        self.assertEqual(self.ingest(session_id, "STALE-THEN-EXPIRED"), 0)
        task = self.task("STALE-THEN-EXPIRED")
        model_cost = task["official_cost"]["by_model"]["gpt-6-luna"]
        self.assertEqual(model_cost["priced"][snapshot["snapshot_id"]]["calls"], 1)
        self.assertTrue(task["official_cost"]["snapshots"][snapshot["snapshot_id"]]["used_while_stale"])

        # Keep the same content-addressed pricing document but age its fetch
        # time past the seven-day maximum. New calls must stay in the token
        # totals and be explicitly unpriced without erasing earlier estimates.
        self.write_official_snapshot(fetched_epoch=now - max_age - 1)
        self.assertEqual(self.mod.load_official_pricing_context()["status"], "expired")
        write_jsonl(self.otel_path(), [span_at(2)])
        self.assertEqual(self.ingest(session_id, "STALE-THEN-EXPIRED"), 0)

        task = self.task("STALE-THEN-EXPIRED")
        model_cost = task["official_cost"]["by_model"]["gpt-6-luna"]
        self.assertEqual(model_cost["priced"][snapshot["snapshot_id"]]["calls"], 1)
        self.assertAlmostEqual(model_cost["priced"][snapshot["snapshot_id"]]["usd"], 200 / 1_000_000)
        self.assertEqual(
            model_cost["unpriced"][self.mod.UNPRICED_SNAPSHOT_TOO_OLD],
            {"calls": 1, "total_tokens": 1200},
        )
        summary = self.mod.summarize_official_cost(task)
        self.assertEqual(summary["totals"]["calls"], 2)
        self.assertEqual(summary["totals"]["priced_calls"], 1)
        self.assertEqual(summary["totals"]["unpriced_calls"], 1)
        self.assertEqual(task["totals"]["call_count"], 2)

    def test_gemini_reasoning_calls_are_unpriced_without_dropping_tokens(self):
        snapshot = self.write_official_snapshot(fetched_epoch=time.time() - 60)
        model = "gemini-3.8-flash"
        session_id = "sess-gemini-reasoning-pricing"
        calls = [
            (1, 50),   # reasoning is a positive subset of output
            (50, 50),  # reasoning equals reported output
            (51, 50),  # reasoning exceeds reported output
        ]
        spans = []
        for index, (reasoning, completion) in enumerate(calls):
            start = self.t0 + timedelta(minutes=index + 1)
            spans.append(otel_span(
                session_id,
                "chat " + model,
                start,
                start + timedelta(seconds=1),
                usage_attrs(model, prompt=100, completion=completion, reasoning=reasoning),
            ))
        write_jsonl(self.otel_path(), spans)
        self.assertEqual(self.ingest(session_id, "GEMINI-REASONING-UNPRICED"), 0)

        task = self.task("GEMINI-REASONING-UNPRICED")
        aggregate = task["by_model"][model]
        self.assertEqual(aggregate["call_count"], 3)
        self.assertEqual(aggregate["prompt_tokens"], 300)
        self.assertEqual(aggregate["completion_tokens"], 150)
        self.assertEqual(aggregate["reasoning_tokens"], 102)
        self.assertEqual(aggregate["total_tokens"], 450)
        reason_record = task["official_cost"]["by_model"][model]["unpriced"][self.mod.UNPRICED_GEMINI_REASONING]
        self.assertEqual(reason_record, {"calls": 3, "total_tokens": 450})
        self.assertNotIn(snapshot["snapshot_id"], task["official_cost"]["by_model"][model]["priced"])

    def test_incremental_calls_keep_their_ingestion_snapshot_cost_and_do_not_duplicate(self):
        first_snapshot = self.write_official_snapshot(fetched_epoch=time.time() - 60)
        session_id = "sess-official-snapshots"
        first = self.t0 + timedelta(minutes=1)
        span = lambda start: otel_span(
            session_id,
            "chat gpt-6-luna",
            start,
            start + timedelta(seconds=1),
            usage_attrs(
                "gpt-6-luna",
                prompt=1000,
                completion=200,
                reasoning=100,
                cache_read=300,
                cache_write=100,
            ),
        )
        write_jsonl(self.otel_path(), [span(first)])
        self.assertEqual(self.ingest(session_id, "OFFICIAL-FROZEN"), 0)
        task = self.task("OFFICIAL-FROZEN")
        first_bucket = task["official_cost"]["by_model"]["gpt-6-luna"]["priced"][first_snapshot["snapshot_id"]]
        self.assertEqual(first_bucket["calls"], 1)
        self.assertAlmostEqual(first_bucket["usd"], 175.5 / 1_000_000)

        # A second official document changes every Luna rate. Only the newly
        # appended call may use those rates; the first per-call estimate stays
        # recorded against its original snapshot.
        changed_markdown = OFFICIAL_PRICING_MARKDOWN.replace(
            "| $0.1 | $0.01 | $0.125 | $0.5 |",
            "| $0.2 | $0.02 | $0.25 | $1 |",
        )
        second_snapshot = self.write_official_snapshot(
            fetched_epoch=time.time(),
            markdown=changed_markdown,
        )
        self.assertNotEqual(first_snapshot["snapshot_id"], second_snapshot["snapshot_id"])
        second = first + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [span(second)])
        self.assertEqual(self.ingest(session_id, "OFFICIAL-FROZEN"), 0)
        self.assertEqual(self.ingest(session_id, "OFFICIAL-FROZEN"), 0)

        task = self.task("OFFICIAL-FROZEN")
        model_cost = task["official_cost"]["by_model"]["gpt-6-luna"]
        self.assertEqual(model_cost["priced"][first_snapshot["snapshot_id"]]["calls"], 1)
        self.assertAlmostEqual(
            model_cost["priced"][first_snapshot["snapshot_id"]]["usd"],
            175.5 / 1_000_000,
        )
        self.assertEqual(model_cost["priced"][second_snapshot["snapshot_id"]]["calls"], 1)
        self.assertAlmostEqual(
            model_cost["priced"][second_snapshot["snapshot_id"]]["usd"],
            351 / 1_000_000,
        )
        summary = self.mod.summarize_official_cost(task)
        self.assertEqual(summary["totals"]["calls"], 2)
        self.assertEqual(summary["totals"]["priced_calls"], 2)
        self.assertEqual(summary["totals"]["legacy_calls"], 0)
        self.assertAlmostEqual(summary["totals"]["usd"], 526.5 / 1_000_000)

        markdown = self.mod.render_markdown(task)
        self.assertIn("Official rate source", markdown)
        self.assertIn("Estimated Cost Coverage", markdown)
        self.assertIn(first_snapshot["snapshot_id"], markdown)
        self.assertIn(second_snapshot["snapshot_id"], markdown)
        self.assertIn(first_snapshot["fetched_at"], markdown)
        self.assertIn(second_snapshot["fetched_at"], markdown)
        self.assertNotIn("Total company fixed charge", markdown)

    def test_legacy_aggregate_calls_remain_unpriced_and_are_never_backfilled(self):
        snapshot = self.write_official_snapshot()
        model = "gpt-6-luna"
        old_agg = self.mod.blank_agg()
        old_agg.update({
            "call_count": 2,
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
        })
        task_id = "OFFICIAL-LEGACY"
        old_task = {
            "task_id": self.mod.normalize_task_id(task_id),
            "totals": dict(old_agg),
            "by_model": {model: dict(old_agg)},
            "by_effort": {"unknown": dict(old_agg)},
            "sessions": [],
        }
        self.mod.atomic_write(
            self.mod.task_path(self.mod.normalize_task_id(task_id)),
            json.dumps(old_task, indent=2),
        )

        session_id = "sess-official-after-legacy"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat " + model,
                start,
                start + timedelta(seconds=1),
                usage_attrs(model, prompt=1000, completion=200),
            ),
        ])
        self.assertEqual(self.ingest(session_id, task_id), 0)

        task = self.task(task_id)
        summary = self.mod.summarize_official_cost(task)
        self.assertEqual(task["totals"]["call_count"], 3)
        self.assertEqual(summary["totals"]["priced_calls"], 1)
        self.assertEqual(summary["totals"]["legacy_calls"], 2)
        self.assertAlmostEqual(
            summary["totals"]["usd"],
            (1000 * 0.1 + 200 * 0.5) / 1_000_000,
        )
        self.assertEqual(
            task["official_cost"]["by_model"][model]["priced"][snapshot["snapshot_id"]]["calls"],
            1,
        )
        markdown = self.mod.render_markdown(task)
        self.assertIn("| gpt-6-luna | 3 | 1 | $0.0002 | 0 | 2 | gpt-6-luna (Default×1) |", markdown)
        self.assertIn("2 call(s) are legacy: recorded before official per-token pricing existed", markdown)
        self.assertNotIn("Total company fixed charge", markdown)


class TestShippedRequestPricingSchema(BaseTestCase):
    """The company request-pricing config must ship valid model+effort rates
    with explicit policy metadata; these checks never read a user's config."""

    def test_shipped_request_pricing_schema_and_rates(self):
        with open(REQUEST_PRICING_PATH, encoding="utf-8") as f:
            raw = json.load(f)

        self.assertEqual(raw["unit"], "model_request")
        self.assertEqual(raw["currency"], "USD")
        self.assertIn("not official GitHub pricing", raw["policy_label"])
        self.assertIsInstance(raw["models"], dict)

        config = self.mod.validate_request_pricing(raw)
        expected_rates = {
            "gpt-6-luna": {"xhigh": 0.04},
            "gpt-6.1-sol": {"medium": 0.21, "xhigh": 0.39},
            "claude-opus-5.5": {"high": 1.82},
            "gemini-3.8-flash": {"low": None},
        }
        for model, rates in expected_rates.items():
            with self.subTest(model=model):
                self.assertEqual(config["models"][model]["rates"], rates)
        self.assertEqual(config["aliases"]["gpt-6-1-sol"], "gpt-6.1-sol")
        self.assertEqual(config["aliases"]["claude-opus-5-5"], "claude-opus-5.5")


class TestFixedRequestChargesFromOtel(BaseTestCase):
    """Fixed charges are per usage-bearing OTEL model request and require
    the joint model+effort counts; token pricing stays an independent report."""

    def test_mixed_models_efforts_and_unpriced_requests_have_exact_totals(self):
        self.write_request_pricing()
        session_id = "sess-fixed-mixed"
        first = self.t0 + timedelta(minutes=1)
        spans = []
        calls = [
            ("gpt-6-luna", "xhigh", 2, 1, 1),
            ("gpt-6.1-sol", "medium", 2, 1, 1),
            ("gpt-6.1-sol", "xhigh", 3, 1, 1),
            ("claude-opus-5.5", "high", 1, 1, 1),
            ("gemini-3.8-flash", "low", 1, 1, 1),
            # Known effort but no configured rate for this model+level.
            ("gpt-6.1-sol", "high", 1, 1, 1),
            # The request must not fall back to any configured effort rate.
            ("gpt-6.1-sol", None, 1, 1, 1),
            # This model is token-priced by the separate legacy table, but
            # has no company fixed-request rate.
            ("gpt-5.4", "medium", 1, 1_000_000, 1_000_000),
        ]
        for model, level, count, prompt, completion in calls:
            for _ in range(count):
                start = first + timedelta(seconds=len(spans) * 2)
                spans.append(otel_span(
                    session_id,
                    "chat " + model,
                    start,
                    start + timedelta(seconds=1),
                    usage_attrs(model, prompt=prompt, completion=completion, level=level),
                ))

        # A chat span with no gen_ai.usage.* attributes is not a recorded
        # usage-bearing request and must not add a request or charge.
        no_usage_start = first + timedelta(seconds=len(spans) * 2)
        spans.append(otel_span(
            session_id,
            "chat gpt-6-luna",
            no_usage_start,
            no_usage_start + timedelta(seconds=1),
            {"gen_ai.response.model": "gpt-6-luna"},
        ))
        write_jsonl(self.otel_path(), spans)

        self.assertEqual(self.ingest(session_id, "FIXED-MIXED"), 0)
        task = self.task("FIXED-MIXED")
        joint = task["by_model_effort"]
        self.assertEqual(task["totals"]["call_count"], 12)
        self.assertEqual(task["by_model"]["gpt-6-luna"]["call_count"], 2)
        self.assertEqual(joint["gpt-6.1-sol"]["measured:medium"]["call_count"], 2)
        self.assertEqual(joint["gpt-6.1-sol"]["measured:xhigh"]["call_count"], 3)

        config = self.mod.load_request_pricing()
        self.assertEqual(config["status"], "ok")
        result = self.mod.compute_fixed_charges(task, config)
        self.assertEqual(result["total_requests"], 12)
        self.assertEqual(result["priced_requests"], 8)
        self.assertEqual(result["unpriced_requests"], 4)
        self.assertEqual(result["unattributed_requests"], 0)
        self.assertEqual(result["by_source"]["measured"]["requests"], 8)
        self.assertAlmostEqual(result["total_charge"], 3.49)
        self.assertFalse(result["complete"])

        rows = {(row["model"], row["effort_key"]): row for row in result["rows"]}
        self.assertAlmostEqual(rows[("gpt-6-luna", "measured:xhigh")]["rate"], 0.04)
        self.assertAlmostEqual(rows[("gpt-6.1-sol", "measured:medium")]["rate"], 0.21)
        self.assertAlmostEqual(rows[("gpt-6.1-sol", "measured:xhigh")]["rate"], 0.39)
        self.assertAlmostEqual(rows[("claude-opus-5.5", "measured:high")]["rate"], 1.82)
        self.assertIn("explicitly unpriced", rows[("gemini-3.8-flash", "measured:low")]["reason"])
        self.assertIn("no configured rate", rows[("gpt-6.1-sol", "measured:high")]["reason"])
        self.assertIn("effort unknown", rows[("gpt-6.1-sol", "unknown")]["reason"])
        self.assertIn("no request rate configured", rows[("gpt-5.4", "measured:medium")]["reason"])

        # The legacy estimate helper remains callable for compatibility,
        # but it is no longer rendered as a second/default bill.
        token_total, _, _ = self.mod.estimate_usd(task["by_model"])
        self.assertAlmostEqual(token_total, 11.75)
        markdown = self.mod.render_markdown(task)
        self.assertIn("Estimated USD cost (official GitHub per-token rates recorded at ingestion", markdown)
        self.assertNotIn("Estimated USD cost (independent pricing table, approximate", markdown)
        self.assertNotIn("Total company fixed charge", markdown)
        self.assertNotIn("$3.4900", markdown)
        self.assertNotIn("$15.2400", markdown)


class TestFixedRequestEffortSourceLabels(BaseTestCase):
    """Measured effort is distinguished from configured/inferred effort
    estimates in both the stored aggregation and rendered charge section."""

    def test_measured_configured_and_inferred_sources_render_distinctly(self):
        self.write_request_pricing()
        first = self.t0 + timedelta(minutes=1)

        configured_session = "sess-fixed-configured"
        write_jsonl(self.events_path(configured_session), [
            {
                "type": "session.start",
                "timestamp": iso(first + timedelta(seconds=1)),
                "data": {"reasoningEffort": "medium"},
            },
            {
                "type": "session.model_change",
                "timestamp": iso(first + timedelta(seconds=15)),
                "data": {"reasoningEffort": "xhigh"},
            },
        ])
        write_jsonl(self.otel_path(), [
            otel_span(
                configured_session,
                "chat gpt-6.1-sol",
                first + timedelta(seconds=10),
                first + timedelta(seconds=11),
                usage_attrs("gpt-6.1-sol", level="medium"),
            ),
            otel_span(
                configured_session,
                "chat gpt-6.1-sol",
                first + timedelta(seconds=20),
                first + timedelta(seconds=21),
                usage_attrs("gpt-6.1-sol"),
            ),
        ])
        self.assertEqual(self.ingest(configured_session, "FIXED-SOURCES"), 0)

        inferred_session = "sess-fixed-inferred"
        write_jsonl(self.events_path(inferred_session), [
            {
                "type": "subagent.started",
                "agentId": "senior-1",
                "timestamp": iso(first + timedelta(seconds=30)),
                "data": {"agentName": "senior-coder"},
            },
            {
                "type": "subagent.completed",
                "agentId": "senior-1",
                "timestamp": iso(first + timedelta(seconds=60)),
                "data": {"agentName": "senior-coder", "totalTokens": 4, "durationMs": 30000},
            },
        ])
        write_jsonl(self.otel_path(), [
            otel_span(
                inferred_session,
                "chat claude-opus-5.5",
                first + timedelta(seconds=40),
                first + timedelta(seconds=41),
                usage_attrs("claude-opus-5.5"),
            ),
        ])
        self.assertEqual(self.ingest(inferred_session, "FIXED-SOURCES"), 0)

        task = self.task("FIXED-SOURCES")
        joint = task["by_model_effort"]
        self.assertEqual(joint["gpt-6.1-sol"]["measured:medium"]["call_count"], 1)
        self.assertEqual(joint["gpt-6.1-sol"]["configured:xhigh"]["call_count"], 1)
        self.assertEqual(joint["claude-opus-5.5"]["inferred:high"]["call_count"], 1)

        result = self.mod.compute_fixed_charges(task, self.mod.load_request_pricing())
        self.assertEqual(result["priced_requests"], 3)
        self.assertAlmostEqual(result["by_source"]["measured"]["charge"], 0.21)
        self.assertAlmostEqual(result["by_source"]["configured"]["charge"], 0.39)
        self.assertAlmostEqual(result["by_source"]["inferred"]["charge"], 1.82)
        self.assertAlmostEqual(result["total_charge"], 2.42)
        self.assertTrue(result["complete"])

        section = "\n".join(self.mod.render_fixed_charge_section(task))
        self.assertIn("| gpt-6.1-sol | measured | medium | 1 | $0.2100 | $0.2100 | priced |", section)
        self.assertIn("priced — ESTIMATE (configured effort, not measured)", section)
        self.assertIn("priced — ESTIMATE (inferred effort, not measured)", section)
        self.assertIn("configured effort — ESTIMATE", section)
        self.assertIn("inferred effort — ESTIMATE", section)
        self.assertIn("never a verified bill", section)
        self.assertIn("$2.4200 — all 3 recorded requests priced", section)


class TestFixedRequestIncrementalIngestion(BaseTestCase):
    """Ingesting appended usage adds only new joint requests; a no-op
    re-ingest does not duplicate their counts or fixed charge."""

    def test_appends_and_reingests_do_not_duplicate_joint_request_counts(self):
        self.write_request_pricing()
        session_id = "sess-fixed-incremental"
        first = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat gpt-6.1-sol",
                first,
                first + timedelta(seconds=1),
                usage_attrs("gpt-6.1-sol", level="medium"),
            ),
        ])

        self.assertEqual(self.ingest(session_id, "FIXED-INCREMENTAL"), 0)
        self.assertEqual(self.ingest(session_id, "FIXED-INCREMENTAL"), 0)
        second = first + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat gpt-6.1-sol",
                second,
                second + timedelta(seconds=1),
                usage_attrs("gpt-6.1-sol", level="xhigh"),
            ),
        ])
        self.assertEqual(self.ingest(session_id, "FIXED-INCREMENTAL"), 0)
        self.assertEqual(self.ingest(session_id, "FIXED-INCREMENTAL"), 0)

        task = self.task("FIXED-INCREMENTAL")
        self.assertEqual(task["totals"]["call_count"], 2)
        self.assertEqual(task["by_model_effort"]["gpt-6.1-sol"]["measured:medium"]["call_count"], 1)
        self.assertEqual(task["by_model_effort"]["gpt-6.1-sol"]["measured:xhigh"]["call_count"], 1)
        result = self.mod.compute_fixed_charges(task, self.mod.load_request_pricing())
        self.assertEqual(result["total_requests"], 2)
        self.assertEqual(result["priced_requests"], 2)
        self.assertAlmostEqual(result["total_charge"], 0.60)


class TestFixedRequestAliasResolution(BaseTestCase):
    """Fixed request charges resolve model aliases without inventing rates."""

    def test_measured_alias_call_uses_the_canonical_model_rate(self):
        # Copy the shipped fixture into this test's private support directory;
        # the test never reads or changes a user's installed pricing file.
        self.write_request_pricing()
        session_id = "sess-fixed-alias"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat gpt-6-1-sol",
                start,
                start + timedelta(seconds=1),
                usage_attrs("gpt-6-1-sol", level="medium"),
            ),
        ])

        self.assertEqual(self.ingest(session_id, "FIXED-ALIAS"), 0)
        task = self.task("FIXED-ALIAS")
        alias_agg = task["by_model_effort"]["gpt-6-1-sol"]["measured:medium"]
        self.assertEqual(alias_agg["call_count"], 1)

        result = self.mod.compute_fixed_charges(task, self.mod.load_request_pricing())
        self.assertEqual(result["priced_requests"], 1)
        self.assertAlmostEqual(result["total_charge"], 0.21, places=4)
        row = next(row for row in result["rows"] if row["model"] == "gpt-6-1-sol")
        self.assertEqual(row["canonical_model"], "gpt-6.1-sol")
        self.assertAlmostEqual(row["rate"], 0.21, places=4)

        section = "\n".join(self.mod.render_fixed_charge_section(task))
        self.assertIn(
            "| gpt-6-1-sol | measured | medium | 1 | $0.2100 | $0.2100 | priced |",
            section,
        )

    def test_null_alias_and_alias_cycle_are_unpriced(self):
        self.write_request_pricing({
            "unit": "model_request",
            "currency": "USD",
            "models": {"canonical-model": {"rates": {"medium": 0.21}}},
            "aliases": {
                "null-alias": None,
                "cycle-a": "cycle-b",
                "cycle-b": "cycle-a",
            },
        })
        task = {
            "totals": {"call_count": 2},
            "by_model": {
                "null-alias": {"call_count": 1},
                "cycle-a": {"call_count": 1},
            },
            "by_model_effort": {
                "null-alias": {"measured:medium": {"call_count": 1}},
                "cycle-a": {"measured:medium": {"call_count": 1}},
            },
        }

        # Computing the report must terminate even for the cyclic alias and
        # must not apply the unrelated canonical model's rate to either call.
        result = self.mod.compute_fixed_charges(task, self.mod.load_request_pricing())
        self.assertEqual(result["priced_requests"], 0)
        self.assertEqual(result["unpriced_requests"], 2)
        self.assertEqual(result["unattributed_requests"], 0)
        self.assertEqual(result["total_charge"], 0.0)
        reasons = {row["model"]: row["reason"] for row in result["rows"]}
        self.assertIn("explicitly unpriced", reasons["null-alias"])
        self.assertIn("alias cycle", reasons["cycle-a"])


class TestLegacyFixedRequestAttribution(BaseTestCase):
    """Old task JSON without joint model+effort counts cannot be backfilled
    from the independent model/effort marginals."""

    def test_legacy_calls_are_unattributed_not_retroactively_priced(self):
        self.write_request_pricing()
        task_id = "FIXED-LEGACY"
        model = "gpt-6-luna"
        old_agg = self.mod.blank_agg()
        old_agg.update({
            "call_count": 1,
            "prompt_tokens": 10,
            "total_tokens": 10,
        })
        old_task = {
            "task_id": self.mod.normalize_task_id(task_id),
            "totals": {
                **old_agg,
                "first_call_ts": None,
                "last_call_ts": None,
                "nano_aiu": 0,
                "premium_requests": 0,
            },
            "by_model": {model: dict(old_agg)},
            # Legacy format had these independent marginals but no
            # by_model_effort field, so its joint split is unknowable.
            "by_effort": {"measured:xhigh": dict(old_agg)},
        }
        self.mod.atomic_write(
            self.mod.task_path(self.mod.normalize_task_id(task_id)),
            json.dumps(old_task, indent=2),
        )

        session_id = "sess-fixed-after-legacy"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat " + model,
                start,
                start + timedelta(seconds=1),
                usage_attrs(model, level="xhigh"),
            ),
        ])
        self.assertEqual(self.ingest(session_id, task_id), 0)

        task = self.task(task_id)
        self.assertEqual(task["totals"]["call_count"], 2)
        self.assertEqual(task["by_model"][model]["call_count"], 2)
        self.assertEqual(task["by_effort"]["measured:xhigh"]["call_count"], 2)
        self.assertEqual(task["by_model_effort"][model]["measured:xhigh"]["call_count"], 1)
        self.assertIn("by_model_effort_since", task)

        result = self.mod.compute_fixed_charges(task, self.mod.load_request_pricing())
        self.assertEqual(result["total_requests"], 2)
        self.assertEqual(result["priced_requests"], 1)
        self.assertEqual(result["unattributed"], {model: 1})
        self.assertEqual(result["unattributed_requests"], 1)
        self.assertAlmostEqual(result["total_charge"], 0.04)
        self.assertFalse(result["complete"])
        section = "\n".join(self.mod.render_fixed_charge_section(task))
        self.assertIn("unattributed (recorded before joint model+effort tracking", section)
        self.assertIn("excludes 0 not-priced and 1 unattributed requests", section)

    def test_old_report_without_joint_counts_stays_unattributed_and_n_a(self):
        self.write_request_pricing()
        task_id = "FIXED-LEGACY-ONLY"
        model = "gpt-6-luna"
        old_agg = self.mod.blank_agg()
        old_agg.update({
            "call_count": 2,
            "prompt_tokens": 20,
            "total_tokens": 20,
        })
        old_task = {
            "task_id": task_id,
            "totals": dict(old_agg),
            "by_model": {model: dict(old_agg)},
            # The independent marginal cannot reconstruct the missing joint
            # model+effort counts, even when it happens to match by_model.
            "by_effort": {"measured:xhigh": dict(old_agg)},
        }
        task_path = self.mod.task_path(task_id)
        self.mod.atomic_write(task_path, json.dumps(old_task, indent=2))
        with open(task_path, "rb") as f:
            original_bytes = f.read()

        task = self.mod.load_json(task_path, {})
        self.assertNotIn("by_model_effort", task)
        markdown = self.mod.render_markdown(task)

        # Rendering an old task report must not silently ingest, migrate, or
        # rewrite joint attribution into the persisted task.
        with open(task_path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)
        self.assertEqual(task["by_model"][model]["call_count"], 2)
        self.assertIn("| gpt-6-luna | 2 | 0 | not priced | 0 | 2 | — |", markdown)
        self.assertIn("2 call(s) are legacy: recorded before official per-token pricing existed", markdown)
        self.assertIn("Estimated USD cost (official GitHub per-token rates recorded at ingestion", markdown)
        self.assertNotIn("Total company fixed charge", markdown)
        self.assertNotIn("| gpt-6-luna | measured | xhigh |", markdown)


class TestRequestPricingAvailabilityAndValidation(BaseTestCase):
    """Missing/invalid policy data must be unavailable, never silently
    rendered as a zero charge or partially applied."""

    def _assert_invalid_numeric_pricing_preserves_token_estimate(self, raw):
        self.write_request_pricing_raw(raw)
        session_id = "sess-invalid-numeric-pricing"
        start = self.t0 + timedelta(minutes=1)
        write_jsonl(self.otel_path(), [
            otel_span(
                session_id,
                "chat gpt-5.4",
                start,
                start + timedelta(seconds=1),
                usage_attrs("gpt-5.4", prompt=1_000_000, completion=1_000_000),
            ),
        ])
        ingest_stderr = io.StringIO()
        with contextlib.redirect_stderr(ingest_stderr):
            self.assertEqual(self.ingest(session_id, "INVALID-PRICING"), 0)
        self.assertEqual(ingest_stderr.getvalue(), "")
        task = self.task("INVALID-PRICING")

        config = self.mod.load_request_pricing()
        self.assertEqual(config["status"], "invalid")
        self.assertTrue(config["error"].startswith("invalid numeric value:"))

        token_total, _, _ = self.mod.estimate_usd(task["by_model"])
        self.assertAlmostEqual(token_total, 11.75)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            markdown = self.mod.render_markdown(task)

        # The fixed-request config and its validation helpers are dormant in
        # the default report, so malformed values cannot inject an obsolete
        # charge section or suppress official per-token coverage.
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("**UNAVAILABLE**", markdown)
        self.assertIn(
            "Estimated USD cost (official GitHub per-token rates recorded at ingestion",
            markdown,
        )
        self.assertNotIn("$11.7500", markdown)
        self.assertNotIn("Total company fixed charge", markdown)

    def test_missing_request_pricing_is_unavailable_not_zero(self):
        self.assertFalse(os.path.exists(self.mod.request_pricing_path()))
        self.assertEqual(self.mod.load_request_pricing()["status"], "missing")
        section = "\n".join(self.mod.render_fixed_charge_section({"totals": {"call_count": 0}}))
        self.assertIn("**UNAVAILABLE**", section)
        self.assertIn("*not* a $0 charge", section)
        self.assertNotIn("$0.0000", section)

    def test_bad_json_and_invalid_rates_reject_the_entire_file(self):
        invalid_inputs = [
            ("invalid JSON", "{not valid JSON"),
            ("negative rate", {"xhigh": -0.01}),
            ("boolean rate", {"xhigh": True}),
            ("string rate", {"xhigh": "0.04"}),
            ("infinite rate", {"xhigh": float("inf")}),
            ("NaN rate", {"xhigh": float("nan")}),
        ]
        for case, bad_rates in invalid_inputs:
            with self.subTest(case=case):
                if case == "invalid JSON":
                    self.write_request_pricing_raw(bad_rates)
                else:
                    self.write_request_pricing({
                        "models": {
                            # A valid entry must not be applied when any
                            # other entry makes the document invalid.
                            "gpt-6-luna": {"rates": {"xhigh": 0.04}},
                            "invalid-model": {"rates": bad_rates},
                        },
                    })
                config = self.mod.load_request_pricing()
                self.assertEqual(config["status"], "invalid")
                self.assertTrue(config.get("error"))

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    section = "\n".join(
                        self.mod.render_fixed_charge_section({
                            "totals": {"call_count": 1},
                            "by_model": {"gpt-6-luna": {"call_count": 1}},
                            "by_model_effort": {
                                "gpt-6-luna": {
                                    "measured:xhigh": {"call_count": 1},
                                },
                            },
                        })
                    )
                self.assertIn("**UNAVAILABLE**", section)
                self.assertIn("whole file is rejected", section)
                self.assertIn("*not* a $0 charge", section)
                self.assertFalse(any(line.startswith("|") for line in section.splitlines()))
                self.assertNotIn("$0.0000", section)

    def test_invalid_utf8_request_pricing_is_reported_as_unreadable(self):
        with open(self.mod.request_pricing_path(), "wb") as f:
            f.write(b'{"models":\xff}')

        config = self.mod.load_request_pricing()
        self.assertEqual(config["status"], "invalid")
        self.assertTrue(config["error"].startswith("unreadable:"), config["error"])
        self.assertNotIn("invalid numeric value", config["error"])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            section = "\n".join(
                self.mod.render_fixed_charge_section({"totals": {"call_count": 1}})
            )
        self.assertIn("**UNAVAILABLE**", section)
        self.assertIn("unreadable:", stderr.getvalue())
        self.assertNotIn("invalid numeric value", stderr.getvalue())

    def test_invalid_unit_currency_schema_and_duplicate_normalized_efforts(self):
        valid_model = {"valid-model": {"rates": {"medium": 0.21}}}
        invalid_documents = [
            ("unit", {
                "unit": "request",
                "currency": "USD",
                "models": valid_model,
            }, "'unit'"),
            ("currency", {
                "unit": "model_request",
                "currency": "EUR",
                "models": valid_model,
            }, "'currency'"),
            ("schema", {
                "unit": "model_request",
                "currency": "USD",
                "models": [],
            }, "'models'"),
            ("case/whitespace duplicate effort levels", {
                "unit": "model_request",
                "currency": "USD",
                "models": {
                    "duplicate-levels": {
                        "rates": {"Medium": 0.21, " medium ": 0.42},
                    },
                },
            }, "duplicate effort level"),
        ]
        for case, document, expected_error in invalid_documents:
            with self.subTest(case=case):
                self.write_request_pricing(document)
                config = self.mod.load_request_pricing()
                self.assertEqual(config["status"], "invalid")
                self.assertIn(expected_error, config["error"])

    def test_valid_all_unpriced_rates_render_n_a_not_zero(self):
        self.write_request_pricing({
            "unit": "model_request",
            "currency": "USD",
            "models": {
                # A null rate is valid policy data, not a $0 rate.
                "gemini-3.8-flash": {"rates": {"low": None}},
            },
            "aliases": {},
        })
        task = {
            "totals": {"call_count": 2},
            "by_model": {
                "gemini-3.8-flash": {"call_count": 1},
                "unknown-model": {"call_count": 1},
            },
            "by_model_effort": {
                "gemini-3.8-flash": {"measured:low": {"call_count": 1}},
                "unknown-model": {"measured:medium": {"call_count": 1}},
            },
        }

        self.assertEqual(self.mod.load_request_pricing()["status"], "ok")
        section = "\n".join(self.mod.render_fixed_charge_section(task))
        self.assertIn(
            "| Total company fixed charge | N/A — none of the recorded requests could be priced",
            section,
        )
        self.assertIn("explicitly unpriced", section)
        self.assertIn("no request rate configured", section)
        self.assertNotIn("| Total company fixed charge | $0.0000", section)

    def test_unrepresentable_integer_rate_is_unavailable_and_preserves_token_estimate(self):
        # This remains below CPython's minimum configurable digit limit, but
        # is too large for math.isfinite() to convert to a float.
        raw = (
            '{"models":{"invalid-model":{"rates":{"xhigh":'
            + ("9" * 400)
            + "}}}}"
        )
        self._assert_invalid_numeric_pricing_preserves_token_estimate(raw)

    def test_overlong_json_integer_is_unavailable_and_preserves_token_estimate(self):
        get_digit_limit = getattr(sys, "get_int_max_str_digits", None)
        if get_digit_limit is None:
            self.skipTest("Python does not expose the integer conversion digit limit")
        digit_limit = get_digit_limit()
        if digit_limit <= 0:
            self.skipTest("Python's integer conversion digit limit is disabled")

        raw = (
            '{"models":{"invalid-model":{"rates":{"xhigh":'
            + ("9" * (digit_limit + 1))
            + "}}}}"
        )
        self._assert_invalid_numeric_pricing_preserves_token_estimate(raw)

    def test_zero_and_null_rates_are_valid_values(self):
        config_path = self.write_request_pricing({
            "models": {
                "free-model": {"rates": {"xhigh": 0.0}},
                "unpriced-model": {"rates": {"low": None}},
            },
        })
        config = self.mod.load_request_pricing()
        self.assertEqual(config["status"], "ok", config)
        self.assertEqual(config["path"], config_path)
        self.assertEqual(config["models"]["free-model"]["rates"]["xhigh"], 0.0)
        self.assertIsNone(config["models"]["unpriced-model"]["rates"]["low"])


@unittest.skip("tests must not inspect the invoking user's real HOME")
class TestInstalledArtifactsOptional(unittest.TestCase):
    """Live-install checks are intentionally skipped: this suite is
    hermetic and must never inspect files under the invoking user's HOME."""

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

    def test_builtin_task_agent_type_infers_low_effort(self):
        session_id = "sess-inferred-task"
        t_start = self.t0 + timedelta(minutes=1)
        t_call = self.t0 + timedelta(minutes=2)
        t_end = self.t0 + timedelta(minutes=3)
        write_jsonl(self.events_path(session_id), [
            {"type": "subagent.started", "agentId": "a1", "timestamp": iso(t_start),
             "data": {"agentType": "task"}},
            {"type": "subagent.completed", "agentId": "a1", "timestamp": iso(t_end),
             "data": {"agentType": "task", "totalTokens": 10, "durationMs": 1000}},
        ])
        write_jsonl(self.otel_path(), [
            otel_span(session_id, "chat claude-sonnet-5", t_call, t_call + timedelta(seconds=1),
                      usage_attrs("claude-sonnet-5", prompt=7, completion=3)),
        ])

        self.ingest(session_id)
        by_effort = self.task()["by_effort"]
        self.assertIn("inferred:low", by_effort)
        self.assertEqual(by_effort["inferred:low"]["prompt_tokens"], 7)
        self.assertFalse(
            any("unknown" in effort for effort in by_effort),
            "a built-in task interval should be routed to inferred:low, not unknown",
        )

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
        self.assertIn("inferred:xhigh", by_effort)
        self.assertEqual(by_effort["inferred:xhigh"]["prompt_tokens"], 7)
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
        child_env = dict(os.environ)
        for var in (
            "COPILOT_HOME",
            "COPILOT_TASK_REPORTS_DIR",
            "COPILOT_TASK_REPORT_HELPER",
            "COPILOT_OTEL_DIR",
        ):
            child_env.pop(var, None)
        child_env["HOME"] = home
        child_env["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"
        for i in range(2):
            code = self._worker_code(home, session_id, ticket, i)
            procs.append(subprocess.Popen([sys.executable, "-c", code],
                                          env=child_env))
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
        full_env["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"
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
