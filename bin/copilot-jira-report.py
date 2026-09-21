#!/usr/bin/env python3
"""
copilot-jira-report.py — Personal helper for copilot-s.

Aggregates GitHub Copilot CLI usage (tokens, model-call time, reasoning
tokens, Copilot-internal AIU units, and an independent estimated USD cost)
per Jira ticket.

Design notes (see docs/architecture.md in the copilot-cli-toolkit repository
for the user-facing summary):

- PRIMARY source of truth for tokens/model/time is the OpenTelemetry span
  stream under ~/.copilot/otel/*.jsonl. Each "chat <model>" span carries
  `gen_ai.conversation.id` == the session id, real `gen_ai.usage.*` fields
  (input/output/reasoning/cache tokens), and the resolved
  `gen_ai.response.model` (falling back to `gen_ai.request.model` if the
  call never got a response, e.g. it failed). This captures every real
  model call for the session's main conversation AND any custom sub-agent
  invocation nested under it (sub-agents share the parent conversation id).
  events.jsonl's `model.model_call_success` events are NOT used for this —
  they only cover small internal utility calls (title/mode/frustration
  detection etc. on cheap auxiliary models), not the primary agent calls.
- events.jsonl (~/.copilot/session-state/<id>/events.jsonl) is used as a
  SECONDARY source, and is ingested INDEPENDENTLY of OTEL: a not-yet-
  existing events.jsonl never blocks OTEL ingestion, and vice versa — each
  source seeds/advances its own offset(s) on its own schedule. It provides:
    - `session.usage_checkpoint` -> cumulative `totalNanoAiu` /
      `totalPremiumRequests` counters. We track the last cumulative value
      seen per session and add only the *delta* on each ingest, so the
      Copilot-internal usage total is correct without double counting. For
      a session resumed after install (i.e. one whose events.jsonl predates
      the install marker), the cursor baseline is seeded from the LAST
      cumulative checkpoint values seen *before* install/the seeded offset
      — never from 0 — so the first post-install checkpoint only books its
      forward delta, not the session's lifetime total.
    - `session.start` / `session.resume` / `session.model_change` -> the
      real, session-level *configured* reasoningEffort setting, tracked as
      a timestamped timeline (not a single "current" value). In practice,
      on this machine, `session.model_change` reliably carries a
      `reasoningEffort` value, but `session.start`/`session.resume` are not
      guaranteed to — the field may be absent depending on Copilot CLI
      version/config. We defend against this rather than assume it's
      always present: only events whose `data.reasoningEffort` is actually
      truthy add a timeline entry; an event missing it is simply skipped
      (never treated as "effort unset"/zero, and never crashes on a
      missing key), so the timeline just keeps whatever was last known
      until a real value shows up. Each OTEL call without its own per-call
      effort attribute is attributed using the most recent configured-effort
      entry AT OR BEFORE that call's own timestamp — never "whatever the
      setting happens to be by the end of the ingest batch" — so a
      mid-session effort change only relabels calls that actually happened
      after it. Pre-install configured-effort history is also seeded into
      this timeline (not discarded), so early post-install calls can still
      be attributed correctly even before any new session.start/resume/
      model_change event fires.
    - `subagent.started` / `subagent.completed` -> paired into CLOSED
      (start, end) time intervals per agent invocation once both events
      have actually been observed. These closed intervals (never an
      open/"currently active" guess) are used for two purposes:
        1. A "By Custom Agent" table built directly from the
           subagent.completed `totalTokens` self-report (informational
           coverage only — NOT added into the overall totals, to avoid
           double counting model calls already tallied from OTEL spans).
        2. A last-resort "inferred:<level>" effort label for calls whose
           timestamp falls inside a closed interval, via a static
           agent-name -> effort mapping. We deliberately never use an
           open/unpaired "currently active agent" stack for this — a stack
           entry can go stale (e.g. a missed/misordered completed event)
           and would then mislabel every later call. Only fully-closed,
           historically-verified intervals are used.
- Effort intent is always labeled with its source, in priority order:
    1. "measured:<level>"  — the real per-call `gen_ai.request.reasoning.level`
       attribute on that exact OTEL chat span (actual telemetry, not guessed).
    2. "configured:<level>" — real session-level reasoningEffort telemetry
       in effect at the time of the call (session.start/resume/model_change).
    3. "inferred:<level>"  — guessed from our custom-agent role mapping,
       only when the call falls inside a fully-closed subagent interval
       AND no configured-effort timeline entry applies at that call's
       timestamp (a real configured value always outranks this guess,
       even for a call that happens to fall inside a closed interval).
    4. "unknown"            — no effort information available.
  We NEVER label an inferred or configured value as measured telemetry.
- Reasoning tokens (`gen_ai.usage.reasoning.output_tokens`) are a SUBSET of
  output/completion tokens, not an addition to them — the USD estimate uses
  output_tokens as-is (already inclusive of reasoning tokens) and never
  adds reasoning tokens a second time.
- Cache tokens: `gen_ai.usage.cache_read.input_tokens` is a subset of
  input_tokens billed (if the pricing table provides a
  `cache_read_per_million` rate) at that cheaper rate; the remaining
  "fresh" input tokens are billed at the normal input rate. If no
  cache-read rate is configured for a model, cache-read tokens fall back to
  the normal input rate (clearly noted, not silently ignored).
- Copilot-internal cost is reported as raw "nano AIU" (from
  session.usage_checkpoint's cumulative `totalNanoAiu`), explicitly labeled
  as NOT USD.
- Estimated USD cost comes from a separate, user-maintainable pricing table
  (model-pricing.json), including a documented `aliases` map (e.g. a
  dated/variant model id -> a priced canonical model id, or explicitly to
  `null` to mean "intentionally unpriced"). Any model still missing pricing
  after alias resolution is reported as "no pricing data" rather than
  silently priced at $0.
- Whole read-modify-write ingest cycles are wrapped in a cross-process file
  lock (best-effort via fcntl.flock) so concurrent copilot-s invocations
  never race on the same state/ticket files. To stay crash-safe, the
  *offset/cursor* state file is written (atomically) BEFORE the ticket
  aggregate file on each ingest, so a crash between the two writes can only
  ever *undercount* a delta (safe to re-run — never double counts), never
  double count it.
- Missing telemetry files (events.jsonl or a referenced OTEL file) never
  cause a permanent backfill hole: if a file doesn't exist yet, we simply
  do NOT seed/advance its offset this run, and retry seeding next time the
  file exists. Truncation/rewind (current size < last-seen size) is
  detected, warned about on stderr, and the offset is safely reset to the
  file's current size (skipping the now-unknown truncated span rather than
  either silently freezing forever or blindly re-reading and risking a
  double count).
- Install-epoch gating applies to BOTH events.jsonl and OTEL files, for any
  session resumed after this feature was installed (i.e. a session whose
  telemetry predates the install marker): each file's first-ever offset is
  seeded to skip straight past its pre-install bytes, and — for OTEL spans
  specifically — every span is ALSO checked directly against the install
  epoch as a defensive backstop (never counted if its start time is before
  install), so gating never depends solely on the byte-offset seek being
  exactly right.
- Failed model calls ARE accounted for when their OTEL span exposes usage
  attributes (some providers report partial usage even on an error/timeout)
  — we do not filter spans by status code, only by presence of usage data.
"""

import argparse
import contextlib
import glob
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

HOME = os.path.expanduser("~")
SUPPORT_DIR = os.path.join(HOME, ".copilot", "jira-reports")
TICKETS_DIR = os.path.join(SUPPORT_DIR, "tickets")
STATE_FILE = os.path.join(SUPPORT_DIR, "session-state.json")
INSTALL_MARKER_FILE = os.path.join(SUPPORT_DIR, "install-marker.json")
PRICING_FILE = os.path.join(SUPPORT_DIR, "model-pricing.json")
LOCK_FILE = os.path.join(SUPPORT_DIR, ".ingest.lock")
REPORTS_DIR = os.environ.get(
    "COPILOT_JIRA_REPORTS_DIR",
    os.path.join(HOME, "Desktop", "CopilotJiraTaskReports"),
)
SESSION_STATE_DIR = os.path.join(HOME, ".copilot", "session-state")
OTEL_DIR = os.path.join(HOME, ".copilot", "otel")

UNASSIGNED = "UNASSIGNED"
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")

# Custom agent -> inferred reasoning-effort intent (NOT measured telemetry).
# Only ever consulted for calls falling inside a fully-CLOSED subagent
# interval (both started+completed observed) — never a live/open guess.
CUSTOM_AGENT_EFFORT_MAP = {
    "planner": "max",
    "reviewer": "max",
    "test-reviewer": "max",
    "coder": "high",
    "fixer": "high",
    "tester": "medium",
}

# Cap on how many closed agent intervals we keep per session, to bound
# memory/state-file size for very long-lived sessions.
MAX_AGENT_INTERVALS = 2000

# Cap on how many configured-reasoning-effort timeline entries we keep per
# session (session.start/resume/model_change history), same rationale as
# MAX_AGENT_INTERVALS above.
MAX_EFFORT_TIMELINE = 2000


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path, content):
    """Write file atomically (write to temp file in same dir, then rename)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def ensure_dirs():
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    os.makedirs(TICKETS_DIR, exist_ok=True)
    os.makedirs(REPORTS_DIR, exist_ok=True)


@contextlib.contextmanager
def ingest_lock():
    """Cross-process lock around the whole read-modify-write ingest cycle.
    Best-effort: if fcntl isn't available, proceeds unlocked."""
    ensure_dirs()
    if fcntl is None:
        yield
        return
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def normalize_jira_key(raw):
    """Uppercase + validate a Jira key. Returns the normalized key, the
    literal UNASSIGNED sentinel, or None if raw is invalid/unusable."""
    if raw is None:
        return None
    key = raw.strip().upper()
    if not key:
        return None
    if key == UNASSIGNED:
        return UNASSIGNED
    if JIRA_KEY_RE.match(key):
        return key
    return None


def get_install_marker():
    """Idempotently ensure the install marker exists, returning it.
    Safe to call from both `ensure-marker` (before the first ever copilot
    run) and lazily from `ingest` (defensive, e.g. if ensure-marker was
    never called for some reason)."""
    ensure_dirs()
    marker = load_json(INSTALL_MARKER_FILE, None)
    if marker and "installed_epoch" in marker:
        return marker
    marker = {"installed_at": now_iso(), "installed_epoch": datetime.now(timezone.utc).timestamp()}
    atomic_write(INSTALL_MARKER_FILE, json.dumps(marker, indent=2))
    return marker


def parse_ts(ts):
    if not ts:
        return None
    try:
        # Events use e.g. "2026-09-18T12:44:13.429Z"
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def otel_ts(pair):
    """OTEL span start/end times are [seconds, nanoseconds]."""
    if not pair or len(pair) != 2:
        return None
    try:
        return float(pair[0]) + float(pair[1]) / 1e9
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Generic append-only file reading with offset + truncation handling
# --------------------------------------------------------------------------

def read_new_lines_safe(path, file_state, label):
    """Read complete new lines from `path` starting at file_state['offset'],
    handling: missing file (no hole - just skip, offset left unset until
    the file exists), and truncation/rewind (warn + resync to current size
    instead of silently stalling forever or risking a double-count reread).

    `file_state` is a dict with 'offset' (int) possibly absent, mutated in
    place. Returns list of raw (still-encoded) lines.
    """
    if not os.path.exists(path):
        # Missing file must not create a future backfill hole: leave the
        # offset unset so the *next* run (once the file exists) seeds
        # cleanly instead of defaulting to 0 and replaying full history.
        return []

    size = os.path.getsize(path)
    offset = file_state.get("offset")
    if offset is None:
        offset = 0
    elif size < offset:
        print(
            "Warning: %s shrank/rewound (was %d bytes, now %d) — treating as "
            "truncated; resuming from current end instead of stalling or "
            "risking a double count. Some events in the gap are lost." % (label, offset, size),
            file=sys.stderr,
        )
        offset = size

    if size <= offset:
        file_state["offset"] = offset
        return []

    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    # Only consume up to the last full line, in case of an in-progress write.
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        file_state["offset"] = offset
        return []
    usable = chunk[: last_nl + 1]
    new_offset = offset + len(usable)
    file_state["offset"] = new_offset
    return [raw for raw in usable.split(b"\n") if raw.strip()]


def seed_jsonl_offset_by_ts(path, install_epoch, ts_extractor):
    """Generic helper: find the byte offset of the first line in `path`
    whose extracted timestamp (via `ts_extractor(parsed_json_line)`) is
    at/after install_epoch. Returns the file's current size (i.e. "nothing
    new to read") if every line is before install_epoch, or None if the
    file doesn't exist yet (caller must leave the offset unset so this is
    retried later, never backfilled as 0/full-history)."""
    if not os.path.exists(path):
        return None
    offset = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        for raw in f:
            line_len = len(raw)
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                pos += line_len
                continue
            ts = ts_extractor(e)
            if ts is not None and ts >= install_epoch:
                offset = pos
                break
            pos += line_len
    return offset


def seed_events_state_for_install(events_path, install_epoch):
    """Seed everything needed to correctly resume a session's events.jsonl
    processing at install_epoch, in a single pass over the file:

      - 'offset': byte offset of the first event at/after install_epoch
        (normal incremental processing starts there).
      - 'checkpoint_baseline': {'nano_aiu', 'premium_requests'} — the LAST
        cumulative session.usage_checkpoint values seen strictly BEFORE
        install_epoch. Without this, a resumed pre-install session's first
        post-install checkpoint would be diffed against a 0 baseline and
        book its entire lifetime total as if it were new usage.
      - 'effort_timeline': every reasoningEffort session.start/resume/
        model_change setting seen strictly BEFORE install_epoch, so a call
        shortly after install (before any new config event fires) can still
        be attributed to whatever effort was already configured going in,
        instead of falling back to "unknown".

    Returns None if events.jsonl doesn't exist yet (caller must leave all
    of this unset/unseeded so it's retried later, never seeded as empty)."""
    if not os.path.exists(events_path):
        return None

    offset = os.path.getsize(events_path)
    offset_found = False
    checkpoint_baseline = {"nano_aiu": 0, "premium_requests": 0}
    effort_timeline = []

    with open(events_path, "rb") as f:
        pos = 0
        for raw in f:
            line_len = len(raw)
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                pos += line_len
                continue
            ts = parse_ts(e.get("timestamp"))
            if not offset_found and ts is not None and ts >= install_epoch:
                offset = pos
                offset_found = True
            if ts is None or ts < install_epoch:
                etype = e.get("type")
                data = e.get("data") or {}
                if etype == "session.usage_checkpoint":
                    total_nano_aiu = data.get("totalNanoAiu") or 0
                    total_premium = data.get("totalPremiumRequests") or 0
                    if total_nano_aiu > checkpoint_baseline["nano_aiu"]:
                        checkpoint_baseline["nano_aiu"] = total_nano_aiu
                    if total_premium > checkpoint_baseline["premium_requests"]:
                        checkpoint_baseline["premium_requests"] = total_premium
                elif etype in ("session.start", "session.resume", "session.model_change"):
                    if data.get("reasoningEffort") and ts is not None:
                        effort_timeline.append({"ts": ts, "effort": data["reasoningEffort"]})
            pos += line_len

    return {
        "offset": offset,
        "checkpoint_baseline": checkpoint_baseline,
        "effort_timeline": effort_timeline[-MAX_EFFORT_TIMELINE:],
    }


# --------------------------------------------------------------------------
# Aggregation helpers
# --------------------------------------------------------------------------

def blank_agg():
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "duration_ms": 0,
    }


def add_agg(dst, mc):
    dst["prompt_tokens"] += mc["prompt_tokens"]
    dst["completion_tokens"] += mc["completion_tokens"]
    dst["reasoning_tokens"] += mc["reasoning_tokens"]
    dst["cache_read_tokens"] += mc["cache_read_tokens"]
    dst["cache_write_tokens"] += mc["cache_write_tokens"]
    dst["total_tokens"] += mc["total_tokens"]
    dst["call_count"] += 1
    dst["duration_ms"] += mc["duration_ms"]


# --------------------------------------------------------------------------
# events.jsonl processing: configured effort timeline, checkpoint deltas,
# closed subagent intervals
# --------------------------------------------------------------------------

def process_events_jsonl(raw_lines, sess_state):
    """Consume new events.jsonl lines. Mutates sess_state in place:
      - sess_state['effort_timeline']: list of {'ts','effort'} configured
        reasoningEffort settings in chronological order (session.start/
        resume/model_change history), used to attribute each OTEL call to
        whatever effort was actually in effect AT THAT CALL'S timestamp —
        never just "whatever the setting happens to be at the end of this
        ingest batch".
      - sess_state['checkpoint_cursor']: {'nano_aiu':int,'premium_requests':int}
        last cumulative values seen (for delta computation)
      - sess_state['closed_intervals']: list of {name,start,end} fully-closed
        subagent invocations
      - sess_state['open_agents']: {agentId: {name,start}} not-yet-completed
        subagent invocations (only ever promoted to closed_intervals; never
        used directly to attribute a call)
    Returns a dict with: nano_aiu_delta, premium_requests_delta,
    agent_summaries (list from subagent.completed, self-reported, for the
    "By Custom Agent" coverage table), repositories (set).
    """
    checkpoint_cursor = sess_state.setdefault("checkpoint_cursor", {"nano_aiu": 0, "premium_requests": 0})
    effort_timeline = sess_state.setdefault("effort_timeline", [])
    closed_intervals = sess_state.setdefault("closed_intervals", [])
    open_agents = sess_state.setdefault("open_agents", {})

    result = {
        "nano_aiu_delta": 0,
        "premium_requests_delta": 0,
        "agent_summaries": [],
        "repositories": set(),
    }

    for raw in raw_lines:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        etype = e.get("type")
        data = e.get("data") or {}
        ts = parse_ts(e.get("timestamp"))

        if etype in ("session.start", "session.resume", "session.model_change"):
            if data.get("reasoningEffort") and ts is not None:
                # Append (not overwrite) so effort_at() can look up whatever
                # was configured AT THE TIME of a given call, instead of
                # collapsing the whole ingest batch to its final value.
                effort_timeline.append({"ts": ts, "effort": data["reasoningEffort"]})
                if len(effort_timeline) > MAX_EFFORT_TIMELINE:
                    del effort_timeline[: len(effort_timeline) - MAX_EFFORT_TIMELINE]
            ctx = data.get("context") or {}
            repo = ctx.get("repository")
            if repo:
                result["repositories"].add(repo)

        elif etype == "subagent.started":
            agent_id = e.get("agentId")
            if agent_id and ts is not None:
                open_agents[agent_id] = {
                    "name": data.get("agentName") or data.get("agentType") or "unknown-agent",
                    "start": ts,
                }

        elif etype == "subagent.completed":
            agent_id = e.get("agentId")
            opened = open_agents.pop(agent_id, None) if agent_id else None
            if opened and ts is not None:
                closed_intervals.append({"name": opened["name"], "start": opened["start"], "end": ts})
                if len(closed_intervals) > MAX_AGENT_INTERVALS:
                    del closed_intervals[: len(closed_intervals) - MAX_AGENT_INTERVALS]
            result["agent_summaries"].append({
                "name": data.get("agentName") or data.get("agentType") or "unknown-agent",
                "model": data.get("model"),
                "total_tokens": data.get("totalTokens") or 0,
                "duration_ms": data.get("durationMs") or 0,
            })

        elif etype == "session.usage_checkpoint":
            total_nano_aiu = data.get("totalNanoAiu") or 0
            total_premium = data.get("totalPremiumRequests") or 0
            # Cumulative counters: only ever add the forward delta so a
            # resumed/re-ingested session never double counts.
            if total_nano_aiu > checkpoint_cursor["nano_aiu"]:
                result["nano_aiu_delta"] += total_nano_aiu - checkpoint_cursor["nano_aiu"]
                checkpoint_cursor["nano_aiu"] = total_nano_aiu
            if total_premium > checkpoint_cursor["premium_requests"]:
                result["premium_requests_delta"] += total_premium - checkpoint_cursor["premium_requests"]
                checkpoint_cursor["premium_requests"] = total_premium

    return result


def effort_at(ts, sess_state):
    """Last-resort configured/inferred effort label for a call at time ts
    (used only when the call's own OTEL span has no per-call reasoning
    level). Priority, matching the documented/spec order (measured is
    handled by the caller before this function is even invoked):
      2. configured session effort *as of that exact call timestamp*
      3. inferred from a fully-closed subagent interval
      4. unknown
    A configured timeline entry always wins over an inferred subagent-role
    guess, even when the call also falls inside a closed subagent interval
    — real session-level telemetry outranks a static name->effort mapping.

    The configured effort is looked up in sess_state['effort_timeline']
    (chronological list of {'ts','effort'}), picking the LAST entry at or
    before `ts` — never just "whatever the setting is right now" — so a
    mid-session (or mid-ingest-batch) effort change only relabels calls
    that actually happened after it, not calls that came before it."""
    timeline = sess_state.get("effort_timeline", [])
    match = None
    for entry in timeline:
        # Timeline is always appended in chronological order (seeded
        # pre-install entries first, then live events as observed), so the
        # last entry with ts <= call ts is the correct "in effect at call
        # time" value.
        if ts is None or entry["ts"] <= ts:
            match = entry
        else:
            break
    if match:
        return "configured:%s" % match["effort"]

    for iv in sess_state.get("closed_intervals", []):
        if ts is not None and iv["start"] <= ts <= iv["end"]:
            mapped = CUSTOM_AGENT_EFFORT_MAP.get(iv["name"])
            if mapped:
                return "inferred:%s" % mapped
            return "inferred:unknown (custom agent '%s' not in mapping)" % iv["name"]

    return "unknown"


# --------------------------------------------------------------------------
# OTEL processing: the primary token/model/time source
# --------------------------------------------------------------------------

def process_otel_span(attrs, start_ts, end_ts, sess_state):
    """Build a model-call record from one 'chat *' OTEL span's attributes,
    or None if it carries no usage data at all (e.g. a span for a call that
    never got far enough to report tokens)."""
    has_usage = any(k.startswith("gen_ai.usage.") for k in attrs)
    if not has_usage:
        return None

    model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model") or "unknown-model"
    prompt_tokens = int(attrs.get("gen_ai.usage.input_tokens") or 0)
    completion_tokens = int(attrs.get("gen_ai.usage.output_tokens") or 0)
    reasoning_tokens = int(attrs.get("gen_ai.usage.reasoning.output_tokens") or 0)
    cache_read_tokens = int(attrs.get("gen_ai.usage.cache_read.input_tokens") or 0)
    cache_write_tokens = int(attrs.get("gen_ai.usage.cache_write.input_tokens") or 0)
    total_tokens = prompt_tokens + completion_tokens
    duration_ms = 0
    if start_ts is not None and end_ts is not None:
        duration_ms = max(0, int((end_ts - start_ts) * 1000))

    reasoning_level = attrs.get("gen_ai.request.reasoning.level")
    if reasoning_level:
        effort_key = "measured:%s" % reasoning_level
    else:
        effort_key = effort_at(start_ts, sess_state)

    mc = {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "duration_ms": duration_ms,
        "effort_key": effort_key,
    }
    return mc


def scan_otel_files(session_id, sess_state, install_epoch):
    """Scan all OTEL files for new 'chat *' spans belonging to this
    session's conversation id, returning a list of model-call dicts.
    Tracks per-file offsets/sizes in sess_state['otel_files'] so repeated
    ingests only process new bytes, and safely resyncs on truncation.

    Install-epoch gating (two layers, both required):
      1) The FIRST time we ever see a given OTEL file, its offset is seeded
         to skip straight to the first span at/after install_epoch (like
         events.jsonl), so a resumed pre-install session doesn't replay its
         entire pre-install OTEL history the moment it's ingested.
      2) As a defensive backstop (OTEL files can in principle contain
         spans from multiple sessions/timeframes, and the byte-offset seed
         is a chronological approximation), every span is ALSO checked
         against install_epoch directly and dropped if its start time is
         before install — so gating never depends solely on getting the
         seek-seek right.
    """
    otel_state = sess_state.setdefault("otel_files", {})
    calls = []
    min_ts = None
    max_ts = None

    existing_paths = sorted(glob.glob(os.path.join(OTEL_DIR, "*.jsonl")))
    for path in existing_paths:
        file_state = otel_state.setdefault(path, {})
        if "offset" not in file_state:
            seeded = seed_jsonl_offset_by_ts(
                path, install_epoch, lambda e: otel_ts(e.get("startTime"))
            )
            # `path` is one of `existing_paths` (just globbed), so it exists;
            # seeded should never be None here, but fall back to 0 (normal
            # first-seen behavior) rather than skipping the file if it is.
            file_state["offset"] = seeded if seeded is not None else 0
        raw_lines = read_new_lines_safe(path, file_state, "OTEL file %s" % path)
        for raw in raw_lines:
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "span":
                continue
            name = e.get("name") or ""
            if not name.startswith("chat"):
                continue
            attrs = e.get("attributes") or {}
            if attrs.get("gen_ai.conversation.id") != session_id:
                continue
            start_ts = otel_ts(e.get("startTime"))
            end_ts = otel_ts(e.get("endTime"))
            if start_ts is not None and start_ts < install_epoch:
                # Defensive backstop: never count a pre-install call even
                # if it slipped past the byte-offset seed above.
                continue
            mc = process_otel_span(attrs, start_ts, end_ts, sess_state)
            if mc is None:
                continue
            calls.append(mc)
            for t in (start_ts, end_ts):
                if t is None:
                    continue
                if min_ts is None or t < min_ts:
                    min_ts = t
                if max_ts is None or t > max_ts:
                    max_ts = t

    # Drop offset bookkeeping for OTEL files that no longer exist, so state
    # doesn't grow forever (they can't produce new data any more).
    for path in list(otel_state.keys()):
        if path not in existing_paths and not os.path.exists(path):
            del otel_state[path]

    return calls, min_ts, max_ts


# --------------------------------------------------------------------------
# Delta building / merging into the per-ticket aggregate
# --------------------------------------------------------------------------

def build_delta(session_id, sess_state, events_path):
    marker = get_install_marker()
    install_epoch = marker["installed_epoch"]

    delta = {
        "by_model": {},
        "by_effort": {},
        "agent_summaries": [],
        "repositories": set(),
        "min_ts": None,
        "max_ts": None,
        "model_calls": 0,
        "nano_aiu_delta": 0,
        "premium_requests_delta": 0,
    }

    # 1) events.jsonl: configured-effort timeline, checkpoint deltas, agent
    #    intervals. This is entirely OPTIONAL and independent of OTEL below
    #    — a missing/not-yet-existing events.jsonl must never suppress OTEL
    #    ingestion (they are separate data sources that happen to live in
    #    different files with different lifecycles).
    events_file_state = sess_state.setdefault("events_offset_state", {})
    if "offset" not in events_file_state:
        # Seed offset (skip pre-install bytes) + checkpoint baseline +
        # pre-install effort-timeline entries in one pass, only once ever
        # per session. If events.jsonl doesn't exist yet, leave everything
        # unseeded so this is retried on the next ingest — OTEL processing
        # below proceeds regardless.
        seeded = seed_events_state_for_install(events_path, install_epoch)
        if seeded is not None:
            events_file_state["offset"] = seeded["offset"]
            cursor = sess_state.setdefault("checkpoint_cursor", {"nano_aiu": 0, "premium_requests": 0})
            # Seed the cursor from the last pre-install cumulative values
            # (never lower it) so the first post-install checkpoint books
            # only its forward delta, not the session's lifetime total.
            cursor["nano_aiu"] = max(cursor["nano_aiu"], seeded["checkpoint_baseline"]["nano_aiu"])
            cursor["premium_requests"] = max(
                cursor["premium_requests"], seeded["checkpoint_baseline"]["premium_requests"]
            )
            timeline = sess_state.setdefault("effort_timeline", [])
            timeline.extend(seeded["effort_timeline"])
            timeline.sort(key=lambda entry: entry["ts"])

    if "offset" in events_file_state:
        raw_lines = read_new_lines_safe(events_path, events_file_state, "events.jsonl for session %s" % session_id)
        ev_result = process_events_jsonl(raw_lines, sess_state)
        delta["nano_aiu_delta"] += ev_result["nano_aiu_delta"]
        delta["premium_requests_delta"] += ev_result["premium_requests_delta"]
        delta["agent_summaries"].extend(ev_result["agent_summaries"])
        delta["repositories"] |= ev_result["repositories"]

    # 2) OTEL: primary token/model/time source — independently ingestible,
    #    gated to install_epoch regardless of events.jsonl's state.
    calls, min_ts, max_ts = scan_otel_files(session_id, sess_state, install_epoch)
    for mc in calls:
        model = mc["model"]
        effort_key = mc["effort_key"]
        delta["by_model"].setdefault(model, blank_agg())
        add_agg(delta["by_model"][model], mc)
        delta["by_effort"].setdefault(effort_key, blank_agg())
        add_agg(delta["by_effort"][effort_key], mc)
        delta["model_calls"] += 1

    delta["min_ts"] = min_ts
    delta["max_ts"] = max_ts

    delta["repositories"] = sorted(delta["repositories"])
    return delta


def merge_delta_into_ticket(ticket, delta, session_id, jira_key):
    ticket.setdefault("jira_key", jira_key)
    ticket.setdefault("created_at", now_iso())
    ticket["updated_at"] = now_iso()

    sessions = set(ticket.get("sessions", []))
    sessions.add(session_id)
    ticket["sessions"] = sorted(sessions)

    repos = set(ticket.get("repositories", []))
    repos.update(delta["repositories"])
    ticket["repositories"] = sorted(repos)

    totals = ticket.setdefault("totals", blank_agg())
    totals.setdefault("first_call_ts", None)
    totals.setdefault("last_call_ts", None)
    totals.setdefault("nano_aiu", 0)
    totals.setdefault("premium_requests", 0)

    by_model = ticket.setdefault("by_model", {})
    for model, agg in delta["by_model"].items():
        dst = by_model.setdefault(model, blank_agg())
        for k in blank_agg():
            dst[k] += agg[k]
            totals[k] = totals.get(k, 0) + agg[k]

    by_effort = ticket.setdefault("by_effort", {})
    for key, agg in delta["by_effort"].items():
        dst = by_effort.setdefault(key, blank_agg())
        for k in blank_agg():
            dst[k] += agg[k]

    by_agent = ticket.setdefault("by_agent", {})
    for summary in delta["agent_summaries"]:
        dst = by_agent.setdefault(summary["name"], {"completions": 0, "total_tokens": 0, "duration_ms": 0})
        dst["completions"] += 1
        dst["total_tokens"] += summary["total_tokens"]
        dst["duration_ms"] += summary["duration_ms"]

    # True calendar span = earliest ever call timestamp -> latest ever call
    # timestamp across all ingests/sessions for this ticket. NEVER sum
    # per-ingest (max_ts - min_ts) deltas across incremental runs — that
    # would either double count overlapping time or understate/misrepresent
    # the actual calendar span depending on how ingests happen to be
    # batched, since each batch's local span isn't additive with another's.
    if delta["min_ts"] is not None:
        if totals["first_call_ts"] is None or delta["min_ts"] < totals["first_call_ts"]:
            totals["first_call_ts"] = delta["min_ts"]
    if delta["max_ts"] is not None:
        if totals["last_call_ts"] is None or delta["max_ts"] > totals["last_call_ts"]:
            totals["last_call_ts"] = delta["max_ts"]

    totals["nano_aiu"] += delta["nano_aiu_delta"]
    totals["premium_requests"] += delta["premium_requests_delta"]
    return ticket


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

def load_pricing():
    pricing = load_json(PRICING_FILE, None)
    if not pricing:
        return {}, {}, "N/A (pricing file missing: %s)" % PRICING_FILE
    return pricing.get("models", {}), pricing.get("aliases", {}), pricing.get("updated_at", "unknown")


def resolve_model_price(model, models, aliases, _seen=None):
    """Look up pricing for `model`, following documented aliases. An alias
    mapping to null/None means "intentionally unpriced" and is a deliberate
    no-pricing-data result, not a fallback to $0."""
    if _seen is None:
        _seen = set()
    if model in _seen:
        return None
    _seen.add(model)
    if model in models:
        return models[model]
    if aliases and model in aliases:
        target = aliases[model]
        if target is None:
            return None
        return resolve_model_price(target, models, aliases, _seen)
    return None


def call_cost(agg, price):
    """Cost for one model's aggregate usage. Reasoning tokens are already
    included in completion_tokens (never added again). Cache-read tokens
    are billed at cache_read_per_million if the pricing entry provides it,
    else fall back to the normal input rate (still counted, not dropped).
    An explicit `cache_read_per_million: null` in the pricing JSON (as
    opposed to the key being absent) is treated the same as absent — it
    also falls back to input_rate — rather than being passed through as
    None and blowing up the arithmetic below."""
    input_rate = price.get("input_per_million", 0)
    output_rate = price.get("output_per_million", 0)
    cache_read_rate = price.get("cache_read_per_million")
    if cache_read_rate is None:
        cache_read_rate = input_rate

    cache_read = min(agg["cache_read_tokens"], agg["prompt_tokens"])
    fresh_input = max(agg["prompt_tokens"] - cache_read, 0)

    cost = (fresh_input / 1_000_000.0) * input_rate
    cost += (cache_read / 1_000_000.0) * cache_read_rate
    cost += (agg["completion_tokens"] / 1_000_000.0) * output_rate
    return cost


def estimate_usd(by_model):
    """Returns (total_usd_or_None, list_of_(model,cost), list_of_unpriced_models)."""
    models, aliases, _ = load_pricing()
    total = 0.0
    priced = []
    unpriced = []
    any_priced = False
    for model, agg in by_model.items():
        price = resolve_model_price(model, models, aliases)
        if not price:
            unpriced.append(model)
            continue
        any_priced = True
        cost = call_cost(agg, price)
        total += cost
        priced.append((model, cost))
    return (total if any_priced else None), priced, unpriced


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def fmt_duration_ms(ms):
    if not ms:
        return "0s"
    s = ms / 1000.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    parts = []
    if h:
        parts.append("%dh" % h)
    if m or h:
        parts.append("%dm" % m)
    parts.append("%.1fs" % sec)
    return " ".join(parts)


def render_markdown(ticket):
    lines = []
    key = ticket.get("jira_key", UNASSIGNED)
    lines.append("# Copilot Jira Usage Report — %s" % key)
    lines.append("")
    lines.append("_Cumulative, incremental report. Tracks only usage recorded since this "
                  "feature was installed on this machine — no historical backfill._")
    lines.append("")
    lines.append("- Report last updated: %s" % ticket.get("updated_at", "unknown"))
    lines.append("- First recorded: %s" % ticket.get("created_at", "unknown"))
    lines.append("- Copilot sessions contributing: %d" % len(ticket.get("sessions", [])))
    repos = ticket.get("repositories", [])
    if repos:
        lines.append("- Repositories: %s" % ", ".join(repos))
    lines.append("")

    totals = ticket.get("totals", blank_agg())
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append("| Model calls (from OTEL chat spans; includes failed calls that reported usage) | %d |" % totals.get("call_count", 0))
    lines.append("| Prompt (input) tokens | %d |" % totals.get("prompt_tokens", 0))
    lines.append("| ...of which cache-read tokens | %d |" % totals.get("cache_read_tokens", 0))
    lines.append("| Cache-write tokens (informational) | %d |" % totals.get("cache_write_tokens", 0))
    lines.append("| Completion (output) tokens | %d |" % totals.get("completion_tokens", 0))
    lines.append("| ...of which reasoning tokens (measured; already included in completion tokens above, not added separately) | %d |" % totals.get("reasoning_tokens", 0))
    lines.append("| Total tokens | %d |" % totals.get("total_tokens", 0))
    lines.append("| Model-call time (sum of OTEL span durations — active model-call time only) | %s |" % fmt_duration_ms(totals.get("duration_ms", 0)))
    first_call_ts = totals.get("first_call_ts")
    last_call_ts = totals.get("last_call_ts")
    if first_call_ts is not None and last_call_ts is not None:
        span_ms = int((last_call_ts - first_call_ts) * 1000)
        lines.append("| Calendar span (earliest recorded call → latest recorded call, across all sessions; includes idle/user-think time, NOT the same as active model-call time above) | %s |" % fmt_duration_ms(span_ms))
    nano_aiu = totals.get("nano_aiu", 0)
    lines.append("| Copilot-internal usage (raw, **not USD**; from session.usage_checkpoint cumulative deltas) | %d nano-AIU (%.6f AIU) |" % (nano_aiu, nano_aiu / 1e9))
    lines.append("| Copilot premium requests consumed (from session.usage_checkpoint cumulative deltas) | %d |" % totals.get("premium_requests", 0))

    total_usd, priced, unpriced = estimate_usd(ticket.get("by_model", {}))
    if total_usd is not None:
        usd_str = "$%.4f" % total_usd
        if unpriced:
            usd_str += " (partial — no pricing data for: %s)" % ", ".join(sorted(set(unpriced)))
        lines.append("| Estimated USD cost (independent pricing table, approximate — NOT an official Copilot bill) | %s |" % usd_str)
    else:
        lines.append("| Estimated USD cost (independent pricing table, approximate) | N/A — no pricing data for any model used (%s) |" % ", ".join(sorted(set(unpriced))))
    lines.append("")

    lines.append("## By Model")
    lines.append("")
    lines.append("| Model | Calls | Prompt | Cache-read | Completion | Reasoning | Total Tokens | Model-call Time | Est. USD |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    models, aliases, _ = load_pricing()
    for model, agg in sorted(ticket.get("by_model", {}).items(), key=lambda kv: -kv[1]["total_tokens"]):
        price = resolve_model_price(model, models, aliases)
        cost_str = "$%.4f" % call_cost(agg, price) if price else "no pricing data"
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %s | %s |" % (
            model, agg["call_count"], agg["prompt_tokens"], agg["cache_read_tokens"],
            agg["completion_tokens"], agg["reasoning_tokens"], agg["total_tokens"],
            fmt_duration_ms(agg["duration_ms"]), cost_str))
    lines.append("")

    lines.append("## By Reasoning-Effort Intent")
    lines.append("")
    lines.append("_Effort intent is reported with its source, in priority order actually "
                  "applied per call: `measured:*` is the real per-call "
                  "`gen_ai.request.reasoning.level` telemetry from that exact model call. "
                  "`configured:*` is the session-level reasoning-effort setting in effect "
                  "at call time (real telemetry, but not call-specific). `inferred:*` is a "
                  "guess based on our custom-agent role mapping "
                  "(planner/reviewer/test-reviewer=max, coder/fixer=high, tester=medium), "
                  "applied only when the call falls inside a fully-completed subagent "
                  "invocation window, and is **not** measured telemetry. `unknown` means no "
                  "effort information was available for that call._")
    lines.append("")
    lines.append("| Effort (source:level) | Calls | Total Tokens | Reasoning Tokens (measured) |")
    lines.append("|---|---|---|---|")
    for key_, agg in sorted(ticket.get("by_effort", {}).items()):
        lines.append("| %s | %d | %d | %d |" % (key_, agg["call_count"], agg["total_tokens"], agg["reasoning_tokens"]))
    lines.append("")

    by_agent = ticket.get("by_agent", {})
    if by_agent:
        lines.append("## By Custom Agent (self-reported coverage — NOT summed into totals)")
        lines.append("")
        lines.append("_From `subagent.completed` events' own `totalTokens`/`durationMs` "
                      "self-report. This is informational coverage only: it is NOT added "
                      "into the Summary/By Model totals above (those already come from the "
                      "underlying OTEL model calls, so adding this too would double count)._")
        lines.append("")
        lines.append("| Agent | Completions | Self-reported Total Tokens | Self-reported Duration |")
        lines.append("|---|---|---|---|")
        for name, agg in sorted(by_agent.items(), key=lambda kv: -kv[1]["total_tokens"]):
            lines.append("| %s | %d | %d | %s |" % (name, agg["completions"], agg["total_tokens"], fmt_duration_ms(agg["duration_ms"])))
        lines.append("")

    lines.append("## Limitations")
    lines.append("")
    lines.append("- Usage is only tracked from the moment this feature was installed; older "
                  "session history is intentionally not backfilled.")
    lines.append("- Token/model/time data comes from local OTEL span files "
                  "(`~/.copilot/otel/*.jsonl`); if a session's OTEL file(s) or events.jsonl "
                  "are deleted before ingestion runs (outside the normal copilot-s flow), "
                  "that increment is lost.")
    lines.append("- \"Reasoning tokens\" are the actual measured reasoning-token usage "
                  "reported for models that expose it; models that don't expose it will "
                  "show 0 here even if they reasoned internally. They are always a subset "
                  "of the completion-token count, never added on top of it.")
    lines.append("- Effort **intent** (measured/configured/inferred) is not the same as "
                  "measured reasoning-token usage — see the table above for actual counts.")
    lines.append("- \"inferred:*\" effort is only applied for calls inside a fully-completed "
                  "subagent window; calls under a subagent invocation whose completion event "
                  "hasn't been observed yet are labeled by configured/unknown effort instead "
                  "of guessed, to avoid misattribution from stale/open state.")
    lines.append("- Copilot-internal AIU/premium-request counts are raw usage accounting "
                  "units, not a currency amount.")
    lines.append("- The estimated USD cost uses an independently maintained pricing table "
                  "(model-pricing.json) with public list-price approximations (including "
                  "alias resolution for renamed/dated model ids); it is not an official "
                  "Copilot invoice and may drift from actual billing. Cache-read tokens are "
                  "priced at a model's `cache_read_per_million` rate when configured, "
                  "otherwise at the normal input rate.")
    lines.append("- If an OTEL file is truncated/rewound (rare), the gap is skipped with a "
                  "warning rather than being silently lost forever or risking a double count.")
    lines.append("")
    return "\n".join(lines)


def ticket_path(jira_key):
    safe = jira_key.replace("/", "_")
    return os.path.join(TICKETS_DIR, "%s.json" % safe)


def report_path(jira_key):
    safe = jira_key.replace("/", "_")
    return os.path.join(REPORTS_DIR, "%s.md" % safe)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ensure_marker(args):
    ensure_dirs()
    marker = get_install_marker()
    print("Install marker: %s" % marker["installed_at"])
    return 0


def cmd_ingest(args):
    ensure_dirs()
    session_id = args.session_id
    session_dir = args.session_dir or os.path.join(SESSION_STATE_DIR, session_id)
    events_path = os.path.join(session_dir, "events.jsonl")

    with ingest_lock():
        state = load_json(STATE_FILE, {})
        sess_state = state.setdefault(session_id, {})

        # --jira may be blank (reuse last known) or invalid (safely fall
        # back rather than silently accepting garbage as a Jira key).
        requested = normalize_jira_key(args.jira) if args.jira else None
        if args.jira and requested is None:
            print("Warning: ignoring invalid Jira key '%s' from caller; falling back to "
                  "last-known/UNASSIGNED." % args.jira, file=sys.stderr)
        jira_key = requested or sess_state.get("jira_key") or UNASSIGNED
        sess_state["jira_key"] = jira_key

        # events.jsonl and OTEL are independent data sources with
        # independent lifecycles: build_delta seeds/reads each of them on
        # its own, so a not-yet-existing events.jsonl never suppresses OTEL
        # ingestion (and vice versa) — see build_delta's docstring/comments.
        delta = build_delta(session_id, sess_state, events_path)

        if (
            delta["model_calls"] == 0
            and delta["nano_aiu_delta"] == 0
            and delta["premium_requests_delta"] == 0
            and not delta["agent_summaries"]
            and not delta["repositories"]
        ):
            # Still persist offsets/cursors so we don't rescan the same
            # bytes forever, but nothing to merge into the ticket. Every
            # mergeable delta field must be checked here — a premium-only
            # or repository-only update (no new model calls/AIU/agent
            # summaries) would otherwise be silently dropped even though
            # build_delta's advanced offsets mean it can never be
            # re-derived on a later ingest.
            atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))
            return 0

        # Crash-safety ordering: persist the advanced offsets/cursors FIRST.
        # If we crash before the ticket write below, the worst case is an
        # undercounted ticket (safe to re-run later without double
        # counting) rather than a double-counted one.
        atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))

        ticket = load_json(ticket_path(jira_key), {})
        ticket = merge_delta_into_ticket(ticket, delta, session_id, jira_key)
        atomic_write(ticket_path(jira_key), json.dumps(ticket, indent=2))

        md = render_markdown(ticket)
        atomic_write(report_path(jira_key), md)
    return 0


def cmd_report(args):
    ensure_dirs()
    jira_key = normalize_jira_key(args.jira_key)
    if jira_key is None:
        # Never fall back to a raw/uppercased-only value here: an invalid
        # key must fail clearly rather than silently mapping to whatever
        # garbage the caller passed (which could read/write the wrong
        # ticket file, or one that looks plausible but was never ingested
        # under that exact form).
        print(
            "Error: '%s' is not a valid Jira key (expected e.g. ABC-123, or the literal "
            "'%s')." % (args.jira_key, UNASSIGNED),
            file=sys.stderr,
        )
        return 1
    tpath = ticket_path(jira_key)
    ticket = load_json(tpath, None)
    if ticket is None:
        print("No recorded usage for Jira ticket '%s' yet." % jira_key, file=sys.stderr)
        return 1
    md = render_markdown(ticket)
    with ingest_lock():
        atomic_write(report_path(jira_key), md)
    print(md)
    print("\n(report file: %s)" % report_path(jira_key), file=sys.stderr)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Copilot Jira usage report helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_marker = sub.add_parser("ensure-marker", help="Idempotently create the install marker (call before the first-ever copilot run)")
    p_marker.set_defaults(func=cmd_ensure_marker)

    p_ingest = sub.add_parser("ingest", help="Ingest new telemetry for a session into its Jira ticket report (also used for the pre-deletion ingest)")
    p_ingest.add_argument("--session-id", required=True)
    p_ingest.add_argument("--jira", default="", help="Jira key for this run (blank = reuse last known / UNASSIGNED)")
    p_ingest.add_argument("--session-dir", default="", help="Override session directory path")
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="Regenerate and print the report for a Jira ticket")
    p_report.add_argument("jira_key", help="Jira key to report on (e.g. ABC-123, or the literal UNASSIGNED); "
                                            "malformed keys are rejected with an error, never silently fixed up")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args) or 0)
    except Exception as ex:  # noqa: BLE001 - this tool must never crash the caller's shell
        print("copilot-jira-report.py error: %s" % ex, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
