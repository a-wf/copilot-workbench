#!/usr/bin/env python3
"""
copilot-task-report.py — Personal helper for copilot-s.

Aggregates GitHub Copilot CLI usage (tokens, model-call time, reasoning
tokens, Copilot-internal AIU units, and an independent estimated USD cost)
per task.

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
- Cache tokens: `gen_ai.usage.cache_read.input_tokens` and
  `gen_ai.usage.cache_write.input_tokens` are SUBSETS of input_tokens
  (verified on real Copilot CLI spans; zero values are omitted by the CLI,
  so an absent cache attribute means 0). Each subset is priced at its own
  official rate and the remaining "fresh" input at the input rate.
- Copilot-internal cost is reported as raw "nano AIU" (from
  session.usage_checkpoint's cumulative `totalNanoAiu`), explicitly labeled
  as NOT USD.
- Estimated USD cost comes from GitHub's OFFICIAL per-token rates
  (docs.github.com "Models and pricing for GitHub Copilot"), fetched
  automatically, strictly validated, and cached for 24h under the support
  directory (see the "Official GitHub Copilot per-token pricing" section
  below). Each call is priced at ingestion (tier from that call's own input
  tokens) and the result is recorded in the task JSON with the snapshot id
  that priced it — never repriced later. Unknown models/tiers/usage shapes
  are reported as unpriced (partial coverage), never $0; calls recorded
  before this existed are "legacy" (token counts only, no backfill). The
  old approximate model-pricing.json table is inactive (kept untouched).
- The former company fixed per-request charge (request-pricing.json) is no
  longer rendered in the default report. Its helpers and the joint
  `by_model_effort` aggregation are kept (dormant/compatible) and the
  config file is preserved.
- Whole read-modify-write ingest cycles are wrapped in a cross-process file
  lock (best-effort via fcntl.flock) so concurrent copilot-s invocations
  never race on the same state/task files. To stay crash-safe, the
  *offset/cursor* state file is written (atomically) BEFORE the task
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
- Attribution (task["attribution"], schema v2): a model call is linked to
  an agent ONLY through explicit OTEL trace context — the chat span's own
  `parentSpanId` (same `traceId`) naming an `invoke_agent` span read in the
  same ingest batch, whose `gen_ai.agent.name`/`github.copilot.agent.type`
  identify the agent. Linked calls go to a per-agent "attributed" bucket
  (OTEL usage, models, invocation-span count, and the sum of the per-call
  official estimates recorded at ingestion). Every other call is booked as
  "unknown" with the specific reason the join failed and is never re-linked;
  usage aggregated before the block existed on a task is frozen as
  "legacy". Calls are never attributed from timestamps, models, session
  membership or agent time windows. A v1 block (no attributed bucket) is
  upgraded losslessly. The block also keeps an informational registry of
  invocations observed in events.jsonl (session id + agentId); registry
  presence is not ownership and is not joined to OTEL attribution. The
  "By Custom Agent" self-report table is unchanged and separate.
- `record-review --input <file>` stores explicit, orchestrator-supplied
  reviewer/test-reviewer round records (task["reviews"]) after strict
  validation against the bounded review policy. They are not telemetry,
  and recording never ingests, fetches pricing or regenerates reports.
- Feature delivery tracking (additive, optional): ingest also fills an
  immutable per-call ledger (task["feature_usage"], keyed by session id +
  traceId + the chat span's own spanId, with the official price frozen at
  ingestion) on top of a frozen, unsplittable pre-ledger baseline.
  `record-delivery` stores explicit feature/cycle lifecycle events and
  confirmed material findings (task["delivery"]); the read-only
  `feature-calls` previews exact call selections; `record-feature-usage`
  links exact ledger calls to one started cycle. Nothing is assigned from
  timestamps, models, agents, sessions or guesses (see the section notes).
"""

import argparse
import contextlib
import filecmp
import glob
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

HOME = os.path.expanduser("~")
SUPPORT_DIR = os.path.join(HOME, ".copilot", "task-reports")
TASKS_DIR = os.path.join(SUPPORT_DIR, "tasks")
STATE_FILE = os.path.join(SUPPORT_DIR, "session-state.json")
INSTALL_MARKER_FILE = os.path.join(SUPPORT_DIR, "install-marker.json")
PRICING_FILE = os.path.join(SUPPORT_DIR, "model-pricing.json")
# Company-configured fixed per-model-request charge table (see
# config/request-pricing.json). Deliberately left as None so the path is
# resolved at CALL time from SUPPORT_DIR (see request_pricing_path()),
# meaning any override of SUPPORT_DIR (e.g. a different HOME, or a caller
# that repoints SUPPORT_DIR) is respected. Set this to an explicit path to
# override the location entirely.
REQUEST_PRICING_FILE = None
REQUEST_PRICING_FILENAME = "request-pricing.json"
LOCK_FILE = os.path.join(SUPPORT_DIR, ".ingest.lock")
REPORTS_DIR = os.environ.get(
    "COPILOT_TASK_REPORTS_DIR",
    os.path.join(HOME, "Desktop", "CopilotTaskReports"),
)
SESSION_STATE_DIR = os.path.join(HOME, ".copilot", "session-state")
OTEL_DIR = os.path.join(HOME, ".copilot", "otel")

UNASSIGNED = "UNASSIGNED"
TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")

# Custom agent or built-in Task route -> inferred reasoning-effort intent
# (NOT measured telemetry).
# Only ever consulted for calls falling inside a fully-CLOSED subagent
# interval (both started+completed observed) — never a live/open guess.
#
# "fixer" is a retired pre-redesign agent (superseded by coder/senior-coder
# handling fixes directly) — kept here only so historical sessions whose
# events.jsonl still references it continue to get a reasonable inferred
# label; it is never installed/loaded by current copilot-instructions.md.
CUSTOM_AGENT_EFFORT_MAP = {
    "planner": "medium",
    "discovery": "low",
    "task": "low",
    "reviewer": "high",
    "test-reviewer": "high",
    "senior-coder": "high",
    "coder": "medium",
    "fixer": "high",
    "tester": "xhigh",
}

# Cap on how many closed agent intervals we keep per session, to bound
# memory/state-file size for very long-lived sessions.
MAX_AGENT_INTERVALS = 2000

# Cap on how many configured-reasoning-effort timeline entries we keep per
# session (session.start/resume/model_change history), same rationale as
# MAX_AGENT_INTERVALS above.
MAX_EFFORT_TIMELINE = 2000

# --------------------------------------------------------------------------
# Model-call -> agent-invocation attribution (additive, versioned)
# --------------------------------------------------------------------------
#
# task["attribution"] (schema ATTRIBUTION_SCHEMA_VERSION) records, for every
# model call ingested after the block was started on a task, WHY it is or is
# not linked to an agent invocation.
#
# The ONLY supported link is explicit OTEL trace context: a "chat *" span's
# own `parentSpanId` (within the same `traceId`) naming an `invoke_agent`
# span that was read in the SAME ingest batch. That parent span carries the
# agent identity (`gen_ai.agent.name`, `github.copilot.agent.type`). Calls
# are deliberately NEVER attributed from timestamps, models, session
# membership, events.jsonl agent windows or "the only open interval"
# guesses; only the call's DIRECT parent is consulted (no ancestor walk).
# A call without that link is booked as unknown with the specific reason the
# join failed (no trace parent on the span / parent not observed in the batch
# / parent is not an invoke_agent span). Calls are never re-linked later.
#
# Calls already aggregated on a task when the block is started are frozen as
# "legacy" (a snapshot of the task totals at that moment) — never re-derived,
# repriced or backfilled. The attribution buckets are exclusive and must
# reconcile with task["totals"]: legacy + unknown + attributed == totals.
#
# Schema history: v1 had no "attributed" bucket (every call unknown under
# ATTRIBUTION_REASON_V1_NO_LINK). A valid v1 block is upgraded in place to v2
# by adding an empty "attributed" bucket — lossless, since v1 never linked a
# call — and its v1 unknown rows are kept verbatim (never re-derived).
#
# The same block also holds an informational registry of agent invocations
# OBSERVED in events.jsonl (`subagent.started`/`subagent.completed`, keyed by
# session id + the event's own `agentId`). Registry presence is not ownership
# of any model call or cost; it is NOT joined to the OTEL attribution (no
# shared identifier is used).
ATTRIBUTION_SCHEMA_VERSION = 2
ATTRIBUTION_SUPPORTED_SCHEMA_VERSIONS = (1, 2)
# Reason recorded by schema v1 for every call (kept so historical rows are
# recognized and labeled; never assigned to newly ingested calls).
ATTRIBUTION_REASON_V1_NO_LINK = (
    "no supported link: OTEL chat spans carry no observed agent/invocation identifier "
    "(timestamps, models and agent intervals are never used to attribute calls)"
)
ATTRIBUTION_REASON_NO_SUPPORTED_LINK = (
    "no trace parent: the chat span has no traceId/parentSpanId, so it cannot be joined to an "
    "invoke_agent span (timestamps, models and agent intervals are never used to attribute calls)"
)
# Reason for calls merged from an ingest delta that carries no attribution
# data at all (older code path). Nothing is known about those calls' span
# linkage, so this reason deliberately makes no claim about trace context.
ATTRIBUTION_REASON_DELTA_WITHOUT_ATTRIBUTION = (
    "no attribution data: the ingest delta carried no attribution block (older code path), so these "
    "calls were never evaluated for an agent link (timestamps, models and agent intervals are never "
    "used to attribute calls)"
)
ATTRIBUTION_REASON_PARENT_NOT_OBSERVED = (
    "parent span not observed: the chat span's parentSpanId did not match any span read in the same "
    "ingest batch (parent not yet exported at ingestion, e.g. a still-running or abnormally ended "
    "agent turn, or exported to an unread file); never re-linked later"
)
ATTRIBUTION_REASON_PARENT_NOT_AGENT = (
    "parent is not an agent span: the chat span's direct parent span is not an invoke_agent span "
    "(ancestors are never walked)"
)
# Agent keys for invoke_agent spans without a gen_ai.agent.name.
ATTRIBUTION_ROOT_AGENT_KEY = "(main session: top-level invoke_agent span without agent name)"
ATTRIBUTION_UNNAMED_AGENT_KEY = "(unnamed nested invoke_agent span)"
ATTRIBUTION_MAX_AGENT_KEY_LEN = 200
ATTRIBUTION_PROVENANCE = {
    "model_calls": "OTEL 'chat *' spans matched by gen_ai.conversation.id == session id",
    "attributed": ("chat span parentSpanId == spanId of an invoke_agent span with the same traceId, read in "
                   "the same ingest batch; agent identity from that span's gen_ai.agent.name / "
                   "github.copilot.agent.type"),
    "observed_invocations": "events.jsonl subagent.started / subagent.completed events keyed by session id + agentId",
}


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
    os.makedirs(TASKS_DIR, exist_ok=True)
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


TASK_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_TASK_ID_LEN = 200


def normalize_task_id(raw):
    """Normalize/validate a task ID or free-form task name.

    Two-tier scheme:
      1. Conservative "KEY-123"-style IDs (e.g. issue-tracker keys,
         auto-detected from a git branch name) are uppercased and returned
         as-is if they match TASK_KEY_RE exactly.
      2. Otherwise, treat the input as a generic, free-form task name/ID:
         trim surrounding whitespace, collapse internal whitespace runs to
         a single hyphen, restrict the character set to letters, digits,
         `.`, `_`, `-` (rejecting path separators, `..` traversal
         sequences, and any control character), cap the length, and
         lower-case the result so two names differing only by case can
         never collide on a case-insensitive filesystem.

    The literal UNASSIGNED sentinel always passes through unchanged
    (case-insensitively matched). Returns None if raw is empty/invalid/
    unusable after sanitization (caller should treat this as "invalid
    input", not silently fall back to UNASSIGNED)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return None
    if s.upper() == UNASSIGNED:
        return UNASSIGNED
    collapsed = re.sub(r"\s+", "-", s)
    if len(collapsed) > MAX_TASK_ID_LEN:
        return None
    upper = collapsed.upper()
    if TASK_KEY_RE.match(upper):
        return upper
    if collapsed and collapsed.strip(".") == "":
        # A name consisting ENTIRELY of dots (".", "..", "...", ...) must
        # never be accepted: "." and ".." are filesystem self/parent-dir
        # references, and while ".." alone is already caught by the
        # substring check just below, a lone "." (or "...", "....", etc.,
        # none of which contain the literal ".." substring) would
        # otherwise slip through as a seemingly-safe generic id.
        return None
    if ".." in collapsed or "/" in collapsed or "\\" in collapsed:
        return None
    if not TASK_ID_SAFE_RE.match(collapsed):
        return None
    if collapsed.startswith("-"):
        # A leading '-' would make the normalized id look like a CLI flag
        # (e.g. "-rf", "--help") to argparse/getopt-style parsers at any
        # call site that isn't scrupulously careful about `--`/`=`-form
        # passing; reject it outright as a second, independent layer of
        # defense on top of every call site being fixed to pass it safely.
        return None
    return collapsed.lower()


# --------------------------------------------------------------------------
# One-time legacy migration (pre-2.0 "jira-reports"/"tickets"/Desktop
# "CopilotJiraTaskReports" layout -> the new generic task-reports layout).
#
# Both migrations below are:
#   - idempotent: a no-op once the legacy source no longer exists, and safe
#     to re-run if a previous attempt only partially completed (already-
#     migrated items are detected by destination existence and skipped,
#     never re-copied/overwritten);
#   - non-destructive: a legacy item is only ever removed once its content
#     is verifiably preserved at the new location (either freshly copied,
#     confirmed byte/JSON-identical to what's already there, or explicitly
#     preserved side-by-side under a conflict-marked name); a legacy
#     top-level directory is only removed once EVERY item in it has been
#     handled with no unresolved failures, otherwise it's left in place so
#     the next invocation can safely retry.
# --------------------------------------------------------------------------

LEGACY_SUPPORT_DIR = os.path.join(HOME, ".copilot", "jira-reports")
LEGACY_TASKS_DIRNAME = "tickets"
LEGACY_DESKTOP_DIR = os.path.join(HOME, "Desktop", "CopilotJiraTaskReports")

# Any legacy entry we don't have specific migration logic for (an unknown
# top-level file/dir in LEGACY_SUPPORT_DIR, or a non-JSON file/subdir
# inside the legacy tickets/ dir) is preserved VERBATIM under this
# clearly-named area rather than being silently dropped when the legacy
# source directory is eventually removed.
LEGACY_UNMIGRATED_DIRNAME = "legacy-unmigrated"

# Bookkeeping file (inside SUPPORT_DIR, next to the live tasks/ dir) that
# records, per legacy tickets/<fname>.json entry, a content fingerprint of
# the data that was ALREADY successfully migrated to the new side. Once an
# entry is recorded here, the new-side task file is the live/authoritative
# copy going forward and is expected to legitimately diverge from the
# (stale) legacy snapshot via normal usage (e.g. new sessions ingested
# after migration) — that divergence must never be re-flagged as a
# migration conflict on a later run (see migrate_legacy_task_storage()).
LEGACY_TICKET_MIGRATION_MARKER_NAME = ".legacy-tickets-migrated.json"

# Bookkeeping file (inside SUPPORT_DIR, next to the live tasks/ dir; NOT
# inside REPORTS_DIR itself, so it survives independently of whatever
# happens to the destination tree) that records, per TOP-LEVEL legacy
# Desktop report entry (keyed by its relative path, e.g. "ABC-1.md"), a
# content fingerprint of the RAW legacy SOURCE file it was migrated from.
#
# A canonical top-level report is rendered from LIVE tasks/<id>.json
# state (see `_legacy_md_content_for_copy`), so its destination is
# expected to legitimately keep changing after migration via completely
# normal, unrelated application activity (new usage ingested, a manual
# `report` re-render, or even a destination that's briefly out of sync
# with its own task JSON, e.g. across a crash between the two writes in
# `cmd_ingest`). None of that is a migration conflict. Once this marker
# records that a given legacy source has already been migrated, later
# runs must skip that entry ENTIRELY — no re-render, no byte comparison
# against the (possibly since-diverged) destination, and no new
# `.legacy-conflict-N` file — for as long as the legacy source itself is
# unchanged and the destination still exists, even while the legacy
# directory remains blocked from removal by some other, unrelated,
# unresolved item. The marker is keyed by the file's SOURCE bytes, never
# by anything derived from live task state, so it can never itself be
# invalidated by ordinary live drift.
DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME = ".legacy-desktop-migrated.json"


def _content_fingerprint(obj):
    """Stable content fingerprint for a JSON-able object, used only to
    detect whether a legacy tickets/<fname>.json entry has already been
    migrated in a previous run (not a cryptographic use)."""
    return hashlib.sha256(json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")).hexdigest()


def _raw_file_fingerprint(path):
    """Stable content fingerprint of a file's raw, on-disk bytes (not a
    cryptographic use). Used only to detect whether a legacy Desktop
    report's SOURCE file has changed since it was last migrated —
    deliberately never derived from any live/current task state, so it
    can never be invalidated by ordinary post-migration drift."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _paths_overlap(a, b):
    """True if realpath-normalized `a` and `b` are the same directory, or
    one is nested (a parent or child) inside the other. Both `os.path.abspath`
    equality checks and naive prefix checks on non-normalized paths would
    miss legitimate overlap introduced via a symlink (e.g. `a` is a
    symlink to a subdirectory of `b`, or vice versa) — realpath resolves
    that before comparing."""
    ra = os.path.realpath(a).rstrip(os.sep) or os.sep
    rb = os.path.realpath(b).rstrip(os.sep) or os.sep
    if ra == rb:
        return True
    return (ra + os.sep).startswith(rb + os.sep) or (rb + os.sep).startswith(ra + os.sep)


def _copy_file_bytes(src, dst):
    """Byte-safe atomic copy (works for JSON and Markdown alike)."""
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    with open(src, "rb") as f:
        data = f.read()
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp_path, dst)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _files_identical_bytes(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _dirs_are_identical(a, b):
    """Recursively verify that directory trees `a` and `b` contain
    exactly the same relative paths with byte-identical file content.
    Used to confirm an already-existing legacy-unmigrated destination
    directory is a COMPLETE, verified copy of the legacy source — never
    just assumed complete from its mere existence — before treating it
    as already preserved. A previous run that crashed/was interrupted
    partway through `shutil.copytree` would leave a partial directory at
    `dst`; this must be detected as NOT identical so the legacy source is
    never dropped on top of it."""
    try:
        cmp = filecmp.dircmp(a, b)
    except OSError:
        return False
    if cmp.left_only or cmp.right_only or cmp.funny_files or cmp.common_funny:
        return False
    _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(
        _dirs_are_identical(os.path.join(a, sub), os.path.join(b, sub)) for sub in cmp.common_dirs
    )


def _preserve_verbatim(src, dst, label, problems):
    """Copy an entry we have no specific migration logic for (file,
    directory, or anything else) verbatim to `dst`, never overwriting an
    already-preserved copy. Any failure (including an unsupported file
    type such as a device/fifo, or a destination that already exists with
    DIFFERENT content) is recorded in `problems` — which the caller uses
    to block deletion of the legacy source — rather than silently
    dropping the entry."""
    try:
        if os.path.islink(src):
            problems.append("%s: symlink not migrated (unsupported), left in place" % label)
            return
        if os.path.isdir(src):
            if os.path.exists(dst):
                if os.path.isdir(dst) and _dirs_are_identical(src, dst):
                    return  # already preserved by a previous run (verified complete)
                problems.append(
                    "%s: legacy-unmigrated destination %s already exists but is not a "
                    "verified-complete copy (possibly left over from an interrupted "
                    "previous run); left in place, nothing overwritten" % (label, dst)
                )
                return
            # Copy to a temp directory alongside `dst` first, then move it
            # into place with a single atomic rename, so a crash/interrupt
            # mid-copytree can never leave a PARTIAL directory sitting at
            # `dst` for a later run to mistake for "already preserved".
            parent = os.path.dirname(dst)
            os.makedirs(parent, exist_ok=True)
            tmp_dst = tempfile.mkdtemp(prefix=".tmp-preserve-", dir=parent)
            try:
                os.rmdir(tmp_dst)  # copytree requires its destination not to exist yet
                shutil.copytree(src, tmp_dst, symlinks=False)
                os.replace(tmp_dst, dst)
            except OSError:
                with contextlib.suppress(OSError):
                    shutil.rmtree(tmp_dst, ignore_errors=True)
                raise
            return
        if os.path.isfile(src):
            if os.path.exists(dst):
                if os.path.isfile(dst) and _files_identical_bytes(src, dst):
                    return  # already preserved, identical content
                problems.append(
                    "%s: legacy-unmigrated destination %s already exists with different "
                    "content; left in place" % (label, dst)
                )
                return
            _copy_file_bytes(src, dst)
            return
        problems.append("%s: unsupported file type, left in place" % label)
    except OSError as ex:
        problems.append("%s: could not preserve (%s)" % (label, ex))


def migrate_legacy_task_storage():
    """Migrate ~/.copilot/jira-reports (pre-2.0) into ~/.copilot/task-reports.

    - install-marker.json / model-pricing.json: copy-if-absent only, never
      overwriting a file already present at the new location (e.g. a
      user's already-edited pricing table). If a file is ALREADY present
      at the new location with DIFFERENT content, the legacy version is
      never silently dropped: it's preserved verbatim under
      ~/.copilot/task-reports/legacy-unmigrated/ (falling back to
      blocking cleanup with a clear warning if that itself can't be
      done safely, e.g. a stale non-identical copy already sits there
      from an interrupted previous run).
    - session-state.json: merged by session id; an id already present in
      the new state file is left untouched; migrated entries have their
      `jira_key` field renamed to `task_id` and normalized to the same
      canonical form used for the on-disk task file. If the file exists
      but is unreadable, invalid JSON, or not a JSON object, this is
      recorded as an unresolved problem (never silently ignored/dropped)
      so the legacy source is preserved and the migration can be retried.
    - tickets/*.json: migrated to tasks/<normalized-task-id>.json, with the
      `jira_key` field (if present) read and converted to `task_id` on
      write, and the written `task_id` field always set to the exact
      canonical id used for the destination filename (never left as a
      stale/un-normalized raw value). A destination that already exists
      with DIFFERENT content is a recorded conflict (both the legacy and
      new files are kept, nothing is overwritten) UNLESS this exact
      legacy snapshot was already successfully migrated by a previous
      run — tracked via a small content-fingerprint marker file — in
      which case the new-side file is understood to be the live,
      authoritative copy that has since legitimately diverged (e.g. via
      normal usage/ingest after migration), and is never re-flagged as a
      conflict just because it no longer matches the stale legacy
      snapshot byte-for-byte.
    - Any top-level entry in the legacy directory other than the ones
      above (unknown files/subdirectories), and any entry inside
      tickets/ that isn't a `.json` file, is never silently discarded:
      it's preserved verbatim under
      ~/.copilot/task-reports/legacy-unmigrated/ (mirroring its original
      relative path), and only counted as handled once that copy is
      verified to exist.

    The legacy directory is removed only if every item above was handled
    with no unresolved conflicts/failures — i.e. every entry, known or
    unknown, is verifiably preserved at a new-side location first.
    """
    if not os.path.isdir(LEGACY_SUPPORT_DIR):
        return
    ensure_dirs()
    problems = []
    handled_top_level = {"install-marker.json", "model-pricing.json", "session-state.json", LEGACY_TASKS_DIRNAME}

    for name, dst in (
        ("install-marker.json", INSTALL_MARKER_FILE),
        ("model-pricing.json", PRICING_FILE),
    ):
        src = os.path.join(LEGACY_SUPPORT_DIR, name)
        if not os.path.isfile(src):
            continue
        if not os.path.exists(dst):
            try:
                _copy_file_bytes(src, dst)
            except OSError as ex:
                problems.append("%s: could not copy (%s)" % (name, ex))
            continue
        if os.path.isfile(dst) and _files_identical_bytes(src, dst):
            continue  # already identical; nothing to preserve, safe to drop the legacy copy
        # A DIFFERENT file already exists at the new location (e.g. a
        # freshly-installed default, or a user's already-edited copy).
        # Never silently drop the legacy version underneath it — preserve
        # it verbatim instead (this also blocks legacy-dir removal via
        # `problems` if that preservation itself can't be completed
        # safely, e.g. a non-identical leftover already occupies the
        # legacy-unmigrated/ destination).
        _preserve_verbatim(
            src,
            os.path.join(SUPPORT_DIR, LEGACY_UNMIGRATED_DIRNAME, name),
            name,
            problems,
        )

    legacy_state_path = os.path.join(LEGACY_SUPPORT_DIR, "session-state.json")
    if os.path.exists(legacy_state_path):
        try:
            with open(legacy_state_path, encoding="utf-8") as f:
                legacy_state = json.load(f)
        except (OSError, ValueError) as ex:
            problems.append("session-state.json: unreadable/invalid JSON (%s), left in place" % ex)
            legacy_state = None
        if legacy_state is not None and not isinstance(legacy_state, dict):
            # A corrupt/non-dict legacy state file must never be treated
            # as "nothing to migrate" (which would let the legacy
            # directory be silently removed along with it) — it's an
            # explicit, visible, blocking problem instead.
            problems.append(
                "session-state.json: unexpected content (expected a JSON object mapping "
                "session id -> state), left in place"
            )
        elif legacy_state:
            try:
                new_state = load_json(STATE_FILE, {})
                changed = False
                for session_id, entry in legacy_state.items():
                    if session_id in new_state:
                        continue  # already tracked in the new file; don't touch it
                    if not isinstance(entry, dict):
                        problems.append(
                            "session-state.json: entry for session '%s' is not a JSON object, "
                            "left in place" % session_id
                        )
                        continue
                    entry = dict(entry)
                    if "jira_key" in entry:
                        entry["task_id"] = entry.pop("jira_key")
                    if entry.get("task_id"):
                        entry["task_id"] = normalize_task_id(entry["task_id"]) or entry["task_id"]
                    new_state[session_id] = entry
                    changed = True
                if changed:
                    atomic_write(STATE_FILE, json.dumps(new_state, indent=2, default=list))
            except (OSError, ValueError) as ex:
                problems.append("session-state.json: could not merge (%s)" % ex)

    legacy_tasks_dir = os.path.join(LEGACY_SUPPORT_DIR, LEGACY_TASKS_DIRNAME)
    if os.path.isdir(legacy_tasks_dir):
        marker_path = os.path.join(SUPPORT_DIR, LEGACY_TICKET_MIGRATION_MARKER_NAME)
        migrated_marker = load_json(marker_path, {})
        if not isinstance(migrated_marker, dict):
            migrated_marker = {}
        marker_changed = False
        for fname in sorted(os.listdir(legacy_tasks_dir)):
            src = os.path.join(legacy_tasks_dir, fname)
            if not fname.endswith(".json"):
                # Unknown file/subdirectory inside tickets/ — never just
                # skipped-and-later-deleted: preserve it verbatim instead.
                _preserve_verbatim(
                    src,
                    os.path.join(SUPPORT_DIR, LEGACY_UNMIGRATED_DIRNAME, LEGACY_TASKS_DIRNAME, fname),
                    "tickets/%s" % fname,
                    problems,
                )
                continue
            data = load_json(src, None)
            if data is None:
                problems.append("tickets/%s: unreadable/invalid JSON, left in place" % fname)
                continue
            legacy_id = data.get("task_id") or data.get("jira_key") or fname[:-len(".json")]
            task_id = normalize_task_id(legacy_id) or normalize_task_id(fname[:-len(".json")]) or UNASSIGNED
            if "jira_key" in data:
                data["task_id"] = data.pop("jira_key")
            # Always the canonical id used for the destination filename —
            # never left as whatever (possibly un-normalized) raw value
            # the legacy JSON happened to already have under "task_id".
            data["task_id"] = task_id
            dst = task_path(task_id)
            fingerprint = _content_fingerprint(data)
            if migrated_marker.get(fname) == fingerprint:
                # This exact legacy snapshot was already migrated by a
                # previous run (recorded below when it was first written
                # or confirmed identical). `dst` is now the LIVE,
                # authoritative task file and may have legitimately
                # diverged from this stale legacy snapshot since then
                # (e.g. new sessions ingested) — that must never be
                # mistaken for a migration conflict on a later run.
                continue
            if os.path.exists(dst):
                existing = load_json(dst, None)
                if existing == data:
                    migrated_marker[fname] = fingerprint
                    marker_changed = True
                    continue  # already migrated
                problems.append(
                    "tickets/%s: %s already exists with different content; "
                    "left legacy file in place (no data overwritten)" % (fname, dst)
                )
                continue
            try:
                atomic_write(dst, json.dumps(data, indent=2))
                migrated_marker[fname] = fingerprint
                marker_changed = True
            except OSError as ex:
                problems.append("tickets/%s: could not write %s (%s)" % (fname, dst, ex))
        if marker_changed:
            try:
                atomic_write(marker_path, json.dumps(migrated_marker, indent=2))
            except OSError:
                pass  # best-effort bookkeeping only; correctness never depends on this succeeding

    # Anything else at the top level of the legacy dir that we don't have
    # specific migration logic for (unknown files/subdirectories) is
    # preserved verbatim rather than being silently swept away by the
    # unconditional rmtree below.
    for name in sorted(os.listdir(LEGACY_SUPPORT_DIR)):
        if name in handled_top_level:
            continue
        _preserve_verbatim(
            os.path.join(LEGACY_SUPPORT_DIR, name),
            os.path.join(SUPPORT_DIR, LEGACY_UNMIGRATED_DIRNAME, name),
            name,
            problems,
        )

    if problems:
        sys.stderr.write(
            "warning: legacy task-report migration (%s) left %d item(s) unresolved; "
            "will retry on next run:\n" % (LEGACY_SUPPORT_DIR, len(problems))
        )
        for p in problems:
            sys.stderr.write("  - %s\n" % p)
        return

    try:
        shutil.rmtree(LEGACY_SUPPORT_DIR)
    except OSError as ex:
        sys.stderr.write(
            "warning: legacy task-report migration finished but could not remove "
            "%s: %s\n" % (LEGACY_SUPPORT_DIR, ex)
        )
        return

    # The legacy source tree is now fully, verifiably gone: prune its
    # ticket-migration provenance marker too. That marker exists ONLY to
    # bridge a migration that was blocked across multiple runs while the
    # legacy source persisted (see LEGACY_TICKET_MIGRATION_MARKER_NAME) —
    # once the source has actually been removed, a LATER reappearance of
    # the legacy directory (e.g. a restored backup) is a brand-new,
    # independent occurrence, not a continuation of that blocked run. It
    # must be treated fresh: full content comparison, conflict detection,
    # and preservation — never silently skipped/dropped via a stale
    # fingerprint left over from a migration that already fully
    # completed and whose source no longer exists.
    with contextlib.suppress(OSError):
        os.remove(os.path.join(SUPPORT_DIR, LEGACY_TICKET_MIGRATION_MARKER_NAME))


_LEGACY_MD_HEADING_RE = re.compile(r"^# Copilot Jira Usage Report( — .*)?$", re.MULTILINE)


def _rewrite_legacy_md_heading(text):
    """Best-effort fallback for a legacy .md report whose corresponding
    tasks/<id>.json state can't be found (so we can't re-render it fully):
    replace ONLY the known old heading line with the current generic
    wording, leaving every other line (in particular all the numeric
    totals) byte-for-byte untouched."""
    return _LEGACY_MD_HEADING_RE.sub(
        lambda m: "# Copilot Task Usage Report" + (m.group(1) or ""), text, count=1
    )


def _legacy_md_content_for_copy(src, fname, is_top_level):
    """Return the bytes that should actually be written for a legacy
    Markdown report being migrated to the new reports directory. Any
    stale pre-2.0 "Jira" heading must never survive into a freshly
    migrated report:

    - If `is_top_level` (the file sits directly at the root of the legacy
      Desktop tree, mirroring the canonical `<task-id>.md` layout that
      `report_path()` actually writes) AND a matching
      tasks/<task_id>.json exists (normal case, since the support-dir
      migration runs before this one), re-render the report from that
      JSON with the current renderer — this guarantees the heading is
      generic AND that the totals shown are exactly what the current
      code considers authoritative for that task.
    - A NESTED copy (anywhere under a subdirectory — e.g. a user's own
      "archive/" folder) is never full-re-rendered from live task JSON
      just because its basename happens to match a task id: it may be a
      point-in-time snapshot whose totals are intentionally different
      from the task's CURRENT state, and blindly re-rendering it would
      silently replace that archived snapshot's real content. Nested
      copies only ever get the same safe, targeted heading-only replace
      used as the top-level fallback below (or are left byte-for-byte
      unchanged if that known heading isn't present).
    - Otherwise (no matching JSON, or a nested file), fall back to a
      targeted string replace of only the known old heading line,
      leaving all other content (totals included) untouched.
    - If the file isn't Markdown, or contains neither a JSON match nor
      the known old heading, it's returned unchanged (verbatim copy).
    """
    if not fname.endswith(".md"):
        with open(src, "rb") as f:
            return f.read()
    if is_top_level:
        candidate_id = fname[: -len(".md")]
        task_data = load_json(task_path(candidate_id), None)
        if task_data is None:
            normalized = normalize_task_id(candidate_id)
            if normalized and normalized != candidate_id:
                task_data = load_json(task_path(normalized), None)
        if task_data is not None:
            return render_markdown(task_data).encode("utf-8")
    with open(src, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    rewritten = _rewrite_legacy_md_heading(text)
    return rewritten.encode("utf-8")


def _legacy_md_preserved_content(src, fname):
    """Return the CONFLICT-PRESERVATION content for a legacy file: at
    most a safe heading-only rewrite of its RAW, on-disk source bytes —
    NEVER a full re-render from live task JSON state, even for a
    canonical top-level `<task-id>.md`.

    This is deliberately separate from `_legacy_md_content_for_copy`
    (the CANONICAL migration content, only ever used to populate a
    destination that doesn't exist yet). Two things depend on this
    content specifically being the raw/near-raw legacy source, not the
    transformed canonical copy:

    - Whether an existing destination is a genuine duplicate of the
      legacy source (safe to drop the legacy copy with no conflict
      file). A top-level canonical destination is, by construction,
      normally in sync with the CURRENT live task state — so comparing
      against a full live-state re-render of that same state would
      almost always spuriously "match" even when the raw legacy source
      holds real, different historical content (e.g. a restored
      point-in-time snapshot with different totals). Comparing against
      the near-raw legacy content instead means a genuine difference is
      never silently swallowed just because a live re-render happens to
      equal the destination.
    - What gets written into a `.legacy-conflict[-N]` file when a
      conflict IS detected: the whole point of that file is to preserve
      the actual historical legacy record for inspection, so it must
      hold the legacy source's own content (heading-only normalized at
      most) — never a re-render of unrelated, current live state.
    """
    if not fname.endswith(".md"):
        with open(src, "rb") as f:
            return f.read()
    with open(src, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return _rewrite_legacy_md_heading(text).encode("utf-8")


def _write_bytes_atomic(dst, data):
    d = os.path.dirname(dst)
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp_path, dst)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _file_bytes_equal(path, data):
    """True if the file at `path` exists and its content is exactly
    `data`. Callers pass the CONFLICT-PRESERVATION content (see
    `_legacy_md_preserved_content`: raw legacy source, at most
    heading-rewritten — never a full live-state re-render), so a file
    already correctly migrated on a previous run (e.g. with its stale
    heading already rewritten) is never mistaken for a fresh conflict
    just because the untransformed legacy source no longer matches it
    byte-for-byte, while a genuinely different raw legacy source is never
    swallowed just because a live re-render would coincidentally match."""
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def _resolve_conflict_dst(reports_dir, root, ext, preserved_bytes):
    """Return `(conflict_dst, already_preserved)` for a legacy file that
    conflicts with a different pre-existing file at its normal
    destination.

    Reuses an existing `<root>.legacy-conflict[-N]<ext>` copy if one
    already holds `preserved_bytes` (the CONFLICT-PRESERVATION content —
    see `_legacy_md_preserved_content`: the raw legacy source, at most
    heading-rewritten, NEVER a full live-state re-render)
    (`already_preserved=True`, caller must not write anything) so that
    re-running the migration — e.g. because the legacy directory remains
    blocked from removal by an unrelated, still-unresolved item elsewhere
    in the tree — never mints a new `.legacy-conflict-N` file for content
    that's already safely preserved. Only when no existing numbered slot
    matches is a fresh, still-unused slot returned
    (`already_preserved=False`), which the caller must then populate."""
    n = 2
    candidate = os.path.join(reports_dir, "%s.legacy-conflict%s" % (root, ext))
    while os.path.exists(candidate):
        if _file_bytes_equal(candidate, preserved_bytes):
            return candidate, True
        candidate = os.path.join(reports_dir, "%s.legacy-conflict-%d%s" % (root, n, ext))
        n += 1
    return candidate, False


def migrate_legacy_desktop_reports():
    """Migrate ~/Desktop/CopilotJiraTaskReports (pre-2.0) into the current
    task reports directory (REPORTS_DIR: the new ~/Desktop/CopilotTaskReports
    default, or a COPILOT_TASK_REPORTS_DIR override).

    Walks the legacy tree RECURSIVELY (subdirectories and all) so nothing
    nested is ever silently skipped-then-deleted:

    - A legacy file with no same-named file at the destination is copied
      over. A stale pre-2.0 "Jira" Markdown heading is normalized to the
      current generic wording in the process — see
      `_legacy_md_content_for_copy` — but a FULL re-render from live
      tasks/<id>.json state (which would replace totals, not just the
      heading) is only ever done for a canonical TOP-LEVEL `<id>.md`
      (directly under the legacy root, mirroring `report_path()`'s flat
      layout); a nested copy (e.g. under a user's own "archive/" folder)
      only ever gets the same safe, heading-only text replace used when
      no matching JSON exists, since it may be an intentional
      point-in-time snapshot whose totals must survive unchanged.
    - A legacy file whose destination already holds either (a) the exact
      same TRANSFORMED content this file would migrate to (i.e. it was
      already correctly migrated by a previous run) or (b) byte-identical
      RAW content, is treated as a duplicate/already-done and simply
      dropped — never flagged as a spurious conflict just because the
      untransformed legacy source no longer matches a destination whose
      heading was already normalized on a prior run.
    - A legacy file that has a same-named but otherwise DIFFERENT file at
      the destination is a genuine conflict: both are preserved (the
      existing destination file is left untouched, the legacy one is
      copied in — using the same transformed content it would otherwise
      migrate as — under a `.legacy-conflict` suffixed name for
      inspection) and a warning is printed — never silently overwritten.
      Re-running the migration while the legacy directory remains blocked
      from removal for an unrelated reason (see `_resolve_conflict_dst`)
      never mints a NEW `.legacy-conflict-N` file for a conflict that's
      already been preserved identically by a previous run.
    - A canonical TOP-LEVEL `<id>.md` entry is additionally tracked by
      content-fingerprint provenance (see
      `DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME`, keyed by the file's raw,
      unmodified legacy SOURCE bytes — never by anything derived from
      live task state). Once such an entry has been handled once (via
      any of the outcomes above), it is never re-rendered, re-compared,
      or re-conflicted on a later run as long as its legacy source is
      unchanged and its destination still exists — even though that
      destination, being rendered from live task state, is expected to
      keep legitimately changing afterward via completely ordinary,
      unrelated application activity. This is what lets the legacy
      directory stay blocked from removal (by some other unrelated
      item) across many runs, with live usage mutating the task JSON
      and its report in between, without ever emitting a repeat warning
      or minting a new conflict file for this entry.
    - Anything that isn't a plain file or directory (e.g. a symlink, a
      device file, a FIFO) is left completely unhandled and recorded as a
      failure, so the legacy tree is never removed out from under it.
    - If LEGACY_DESKTOP_DIR itself is a symlink, it is NEVER followed for
      migration. If it resolves (via realpath) to REPORTS_DIR itself,
      only the symlink is removed (the identical real directory/content
      it points to is left completely untouched); otherwise it's left in
      place entirely with a clear warning (no reading, copying, or
      deletion of whatever it points to).
    - If LEGACY_DESKTOP_DIR and REPORTS_DIR overlap after realpath
      normalization — the same directory, or one nested inside the other
      (including via an intermediate symlink) — migration is skipped
      entirely with a clear warning: no files are copied, and the legacy
      directory is never touched/deleted.

    The legacy directory is removed only once every entry (recursively)
    has been handled with no copy failures (conflicts are not failures:
    both copies exist, so nothing is lost by removing the now-empty
    legacy directory).
    """
    if os.path.islink(LEGACY_DESKTOP_DIR):
        resolved = os.path.realpath(LEGACY_DESKTOP_DIR)
        if resolved == os.path.realpath(REPORTS_DIR):
            # The legacy path is just an alias for the current reports
            # directory (e.g. a symlink left over from an older install
            # layout) — remove ONLY the symlink itself; the real
            # directory/content it points to IS REPORTS_DIR and must
            # never be touched.
            try:
                os.unlink(LEGACY_DESKTOP_DIR)
            except OSError as ex:
                sys.stderr.write(
                    "warning: could not remove legacy Desktop symlink %s: %s\n" % (LEGACY_DESKTOP_DIR, ex)
                )
            return
        sys.stderr.write(
            "warning: legacy Desktop reports path %s is a symlink (to %s); a symlinked "
            "legacy directory is never followed or migrated automatically, and its target "
            "is left untouched. Resolve manually if migration is needed.\n"
            % (LEGACY_DESKTOP_DIR, resolved)
        )
        return
    if not os.path.isdir(LEGACY_DESKTOP_DIR):
        return
    if _paths_overlap(LEGACY_DESKTOP_DIR, REPORTS_DIR):
        sys.stderr.write(
            "warning: legacy Desktop reports path %s overlaps with the current reports "
            "directory %s (same directory, or one nested inside the other); skipping "
            "migration — nothing copied, nothing deleted.\n" % (LEGACY_DESKTOP_DIR, REPORTS_DIR)
        )
        return
    os.makedirs(REPORTS_DIR, exist_ok=True)
    conflicts = []
    failures = []

    # Provenance for top-level entries only (see
    # DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME) — deliberately stored under
    # SUPPORT_DIR, not REPORTS_DIR, so it survives independently of the
    # destination tree and of whatever blocks legacy-dir removal.
    marker_path = os.path.join(SUPPORT_DIR, DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME)
    migrated_marker = load_json(marker_path, {})
    if not isinstance(migrated_marker, dict):
        migrated_marker = {}
    marker_changed = False

    for dirpath, dirnames, filenames in os.walk(LEGACY_DESKTOP_DIR):
        rel_dir = os.path.relpath(dirpath, LEGACY_DESKTOP_DIR)
        is_top_level = rel_dir == "."
        # A symlinked subdirectory would otherwise be silently skipped by
        # os.walk (it doesn't descend into it without followlinks=True)
        # and then swept away by the final rmtree — flag it explicitly.
        for dname in dirnames:
            full = os.path.join(dirpath, dname)
            if os.path.islink(full):
                rel = dname if rel_dir == "." else os.path.join(rel_dir, dname)
                failures.append("%s: symlinked directory not migrated (unsupported), left in place" % rel)

        if not is_top_level:
            # A directory containing no files anywhere in its own subtree
            # (e.g. an empty folder, or one holding only further empty
            # subfolders) would otherwise never get a destination created
            # for it — `_write_bytes_atomic` only creates directories as
            # a side effect of writing a FILE into them — silently
            # dropping that (empty) structure once the legacy tree is
            # removed below. Create it explicitly so structure is never
            # lost purely because it happened to be empty.
            dst_dir = os.path.join(REPORTS_DIR, rel_dir)
            try:
                os.makedirs(dst_dir, exist_ok=True)
            except OSError as ex:
                failures.append("%s: could not create directory: %s" % (rel_dir, ex))

        for fname in sorted(filenames):
            src = os.path.join(dirpath, fname)
            rel_path = fname if rel_dir == "." else os.path.join(rel_dir, fname)
            if os.path.islink(src) or not os.path.isfile(src):
                failures.append("%s: unsupported file type, left in place" % rel_path)
                continue
            dst = os.path.join(REPORTS_DIR, rel_path)
            try:
                legacy_fingerprint = _raw_file_fingerprint(src) if is_top_level else None
                if (
                    is_top_level
                    and migrated_marker.get(rel_path) == legacy_fingerprint
                    and os.path.exists(dst)
                ):
                    # Provenance hit: this exact legacy source was already
                    # migrated by a previous run and its destination still
                    # exists. `dst` is a canonical top-level report — it's
                    # expected to keep legitimately diverging afterward via
                    # completely ordinary, unrelated application activity
                    # (new usage ingested, a manual re-render, etc.), and
                    # that divergence must never be re-rendered against,
                    # re-compared, re-flagged as a conflict, or re-warned
                    # about — regardless of how many times this runs while
                    # the legacy directory stays blocked from removal by
                    # some other unrelated item.
                    continue
                # `canonical_content` is what gets WRITTEN to `dst` the
                # first time it's created (a top-level entry with a
                # matching task JSON may get a full live-state re-render —
                # see `_legacy_md_content_for_copy`). `preserved_content`
                # is the separate, near-raw legacy content (at most
                # heading-rewritten, never a live re-render — see
                # `_legacy_md_preserved_content`) used for every
                # comparison against an ALREADY-EXISTING destination, and
                # for whatever gets written into a `.legacy-conflict`
                # file. Keeping these separate ensures a top-level file's
                # canonical live re-render — which, by construction, is
                # normally in sync with `dst` — can never make a
                # genuinely different raw legacy source get silently
                # swallowed as "already migrated", and ensures a
                # `.legacy-conflict` file always preserves the actual
                # historical legacy record, never a re-render of
                # unrelated live state.
                canonical_content = _legacy_md_content_for_copy(src, fname, is_top_level)
                preserved_content = _legacy_md_preserved_content(src, fname)
                if not os.path.exists(dst):
                    _write_bytes_atomic(dst, canonical_content)
                elif _file_bytes_equal(dst, preserved_content):
                    pass  # already correctly migrated (this run or a previous one)
                elif _files_identical_bytes(src, dst):
                    pass  # raw duplicate; legacy copy can be safely dropped
                else:
                    root, ext = os.path.splitext(rel_path)
                    conflict_dst, already_preserved = _resolve_conflict_dst(REPORTS_DIR, root, ext, preserved_content)
                    if not already_preserved:
                        _write_bytes_atomic(conflict_dst, preserved_content)
                    conflicts.append("%s: both copies preserved (existing %s kept, legacy copy is now %s)"
                                      % (rel_path, dst, conflict_dst))
                if is_top_level and migrated_marker.get(rel_path) != legacy_fingerprint:
                    # Record provenance now that this entry has been
                    # handled (however it resolved) so a later rerun never
                    # touches it again while its legacy source is unchanged.
                    migrated_marker[rel_path] = legacy_fingerprint
                    marker_changed = True
            except OSError as ex:
                failures.append("%s: %s" % (rel_path, ex))

    if marker_changed:
        try:
            atomic_write(marker_path, json.dumps(migrated_marker, indent=2))
        except OSError:
            pass  # best-effort bookkeeping only; correctness never depends on this succeeding

    if conflicts:
        sys.stderr.write(
            "warning: Desktop task-report migration found %d file(s) with conflicting "
            "content; both copies were preserved under %s:\n" % (len(conflicts), REPORTS_DIR)
        )
        for c in conflicts:
            sys.stderr.write("  - %s\n" % c)

    if failures:
        sys.stderr.write(
            "warning: Desktop task-report migration could not copy %d file(s); "
            "leaving %s in place to retry next run:\n" % (len(failures), LEGACY_DESKTOP_DIR)
        )
        for f in failures:
            sys.stderr.write("  - %s\n" % f)
        return

    try:
        shutil.rmtree(LEGACY_DESKTOP_DIR)
    except OSError as ex:
        sys.stderr.write(
            "warning: Desktop task-report migration finished but could not remove "
            "%s: %s\n" % (LEGACY_DESKTOP_DIR, ex)
        )
        return

    # The legacy source tree is now fully, verifiably gone: prune its
    # top-level provenance marker too. That marker exists ONLY to bridge
    # a migration that was blocked across multiple runs while the legacy
    # source persisted (see DESKTOP_TOPLEVEL_MIGRATION_MARKER_NAME) — once
    # the source has actually been removed, a LATER reappearance of the
    # legacy directory (e.g. a restored backup) is a brand-new,
    # independent occurrence, not a continuation of that blocked run. It
    # must be treated fresh: full content comparison, conflict detection,
    # and preservation — never silently skipped/dropped via a stale
    # fingerprint left over from a migration that already fully completed
    # and whose source no longer exists.
    with contextlib.suppress(OSError):
        os.remove(marker_path)


def run_startup_migrations():
    """Run both legacy migrations once, guarded by the same cross-process
    lock used for ingest so a concurrent copilot-s invocation can never
    race on the same legacy/new files. Cheap to call on every invocation:
    both migrations return immediately once their legacy source is gone."""
    with ingest_lock():
        migrate_legacy_task_storage()
        migrate_legacy_desktop_reports()


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
        value = float(pair[0]) + float(pair[1]) / 1e9
    except (TypeError, ValueError, OverflowError):
        return None
    # Non-finite (NaN/Infinity) span times are unknown, never a timestamp.
    return value if math.isfinite(value) else None


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
            except (ValueError, RecursionError):  # malformed line (incl. JSONDecodeError, huge ints): skip
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
            except (ValueError, RecursionError):  # malformed line (incl. JSONDecodeError, huge ints): skip
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
    "By Custom Agent" coverage table), repositories (set), and
    invocation_events (observed subagent.started/completed events that carry
    their own `agentId`, for the informational invocation registry; events
    without an agentId are not registered — no identifier is invented).
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
        "invocation_events": [],
    }

    for raw in raw_lines:
        try:
            e = json.loads(raw)
        except (ValueError, RecursionError):  # malformed line (incl. JSONDecodeError, huge ints): skip
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
            if isinstance(agent_id, str) and agent_id:
                result["invocation_events"].append({
                    "agent_id": agent_id,
                    "event": "started",
                    "ts": ts,
                    "agent_name": data.get("agentName") or data.get("agentType") or "unknown-agent",
                    "model": None,
                })

        elif etype == "subagent.completed":
            agent_id = e.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                result["invocation_events"].append({
                    "agent_id": agent_id,
                    "event": "completed",
                    "ts": ts,
                    "agent_name": data.get("agentName") or data.get("agentType") or "unknown-agent",
                    "model": data.get("model") if isinstance(data.get("model"), str) else None,
                })
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
    # Same lenient int(value or 0) conversion as before for every value it
    # already accepted; a value it cannot convert (NaN/Infinity, non-numeric
    # string, list, ...) no longer crashes the ingest: it fails closed to 0
    # in the aggregates, marks the call unpriceable (UNPRICED_INVALID_USAGE,
    # never priced as 0 tokens) and fails usage_strict_ok (unledgered).
    unconvertible = []
    prompt_tokens = _lenient_usage_count(attrs, "gen_ai.usage.input_tokens", unconvertible)
    completion_tokens = _lenient_usage_count(attrs, "gen_ai.usage.output_tokens", unconvertible)
    reasoning_tokens = _lenient_usage_count(attrs, "gen_ai.usage.reasoning.output_tokens", unconvertible)
    cache_read_tokens = _lenient_usage_count(attrs, "gen_ai.usage.cache_read.input_tokens", unconvertible)
    cache_write_tokens = _lenient_usage_count(attrs, "gen_ai.usage.cache_write.input_tokens", unconvertible)
    total_tokens = prompt_tokens + completion_tokens
    duration_ms = 0
    if start_ts is not None and end_ts is not None:
        span_seconds = end_ts - start_ts
        # Non-finite span times (NaN/Infinity in the telemetry) give no
        # duration rather than crashing the ingest.
        if math.isfinite(span_seconds):
            duration_ms = max(0, int(span_seconds * 1000))

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
        # Which usage attributes were actually present on the span (vs.
        # defaulted to 0 above). Per-call official pricing uses this so a
        # MISSING input/output count is never silently priced as 0 tokens.
        # Not aggregated (add_agg ignores it).
        "usage_present": {
            "input": "gen_ai.usage.input_tokens" in attrs,
            "output": "gen_ai.usage.output_tokens" in attrs,
            "reasoning": "gen_ai.usage.reasoning.output_tokens" in attrs,
            "cache_read": "gen_ai.usage.cache_read.input_tokens" in attrs,
            "cache_write": "gen_ai.usage.cache_write.input_tokens" in attrs,
        },
        # Strict validity of the raw usage attributes (finite, non-negative,
        # integral, at most 2**53, never a boolean or string). The aggregate
        # counts above keep the lenient conversion; a call that fails this
        # check is never priced (UNPRICED_INVALID_USAGE) and is booked as
        # unledgered in the call ledger, never "repaired".
        "usage_strict_ok": not unconvertible and all(
            _strict_count(attrs[k]) is not None for k in LEDGER_USAGE_ATTRS if k in attrs
        ),
        # Usage attributes the lenient conversion above could not read (kept
        # as 0 in the aggregates); such a call is never priced.
        "usage_unconvertible": sorted(unconvertible),
    }
    return mc


def _lenient_usage_count(attrs, key, unconvertible):
    """Existing `int(value or 0)` usage conversion, failing closed: a value
    it cannot convert is recorded in `unconvertible` and counted as 0."""
    try:
        return int(attrs.get(key) or 0)
    except (TypeError, ValueError, OverflowError):
        unconvertible.append(key)
        return 0


def scan_otel_files(session_id, sess_state, install_epoch):
    """Scan all OTEL files for new 'chat *' spans belonging to this
    session's conversation id. Returns (calls, min_ts, max_ts, agent_spans):
    model-call dicts (each with its explicit trace-context resolution under
    mc["attribution"], see resolve_call_attribution) and the agent identity
    of each of this session's new `invoke_agent` spans.
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
    # Explicit trace-context join (see attribution notes at the top): every
    # span read in THIS batch, keyed by (traceId, spanId), so each chat call
    # can be resolved against its direct parent once all new bytes are read.
    # Only identifiers actually present on the spans are used.
    span_index = {}
    agent_spans = []

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
            except (ValueError, RecursionError):  # malformed line (incl. JSONDecodeError, huge ints): skip
                continue
            if not isinstance(e, dict) or e.get("type") != "span":
                continue
            name = e.get("name") or ""
            if not isinstance(name, str):
                name = ""
            attrs = e.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            span_key = _trace_span_key(e.get("traceId"), e.get("spanId"))
            agent_info = _invoke_agent_info(name, attrs, e.get("parentSpanId"))
            if span_key is not None and span_key not in span_index:
                span_index[span_key] = agent_info
            if attrs.get("gen_ai.conversation.id") != session_id:
                continue
            start_ts = otel_ts(e.get("startTime"))
            end_ts = otel_ts(e.get("endTime"))
            if start_ts is not None and start_ts < install_epoch:
                # Defensive backstop: never count a pre-install call even
                # if it slipped past the byte-offset seed above.
                continue
            if agent_info is not None:
                # Observed invocation span of this session (counted once:
                # each byte range is read exactly once per session). The
                # span's own ids are carried along (copy) for the
                # feature-usage invocation ledger only.
                agent_spans.append(dict(agent_info, trace_id=e.get("traceId"), span_id=e.get("spanId"),
                                        parent_span_id=e.get("parentSpanId"), start_ts=start_ts))
                continue
            if not name.startswith("chat"):
                continue
            mc = process_otel_span(attrs, start_ts, end_ts, sess_state)
            if mc is None:
                continue
            mc["trace_id"] = e.get("traceId")
            mc["parent_span_id"] = e.get("parentSpanId")
            # The chat span's OWN id (with session + trace id) is the stable
            # call-ledger key; it is validated, never invented, when the
            # ledger delta is built.
            mc["span_id"] = e.get("spanId")
            mc["start_ts"] = start_ts
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

    for mc in calls:
        mc["attribution"] = resolve_call_attribution(mc, span_index)

    return calls, min_ts, max_ts, agent_spans


def _trace_span_key(trace_id, span_id):
    """(traceId, spanId) lookup key, or None unless both are non-empty strings."""
    if isinstance(trace_id, str) and trace_id and isinstance(span_id, str) and span_id:
        return (trace_id, span_id)
    return None


def _invoke_agent_info(name, attrs, parent_span_id):
    """Agent identity carried by an `invoke_agent` span, or None for any
    other span. Identity comes only from the span's own attributes."""
    operation = attrs.get("gen_ai.operation.name")
    if operation != "invoke_agent" and not (name == "invoke_agent" or name.startswith("invoke_agent ")):
        return None
    agent_name = attrs.get("gen_ai.agent.name")
    agent_type = attrs.get("github.copilot.agent.type")
    agent_type = agent_type if isinstance(agent_type, str) and agent_type else None
    if isinstance(agent_name, str) and agent_name.strip():
        key = agent_name.strip()[:ATTRIBUTION_MAX_AGENT_KEY_LEN]
    elif isinstance(parent_span_id, str) and parent_span_id:
        key = ATTRIBUTION_UNNAMED_AGENT_KEY
    else:
        key = ATTRIBUTION_ROOT_AGENT_KEY
    return {"agent_key": key, "agent_type": agent_type}


def resolve_call_attribution(mc, span_index):
    """Resolve model call `mc` to an agent via explicit trace context only.

    Returns {"agent_key": ..., "agent_type": ...} when the call's DIRECT
    parent (same traceId, its own parentSpanId) is an invoke_agent span read
    in the same ingest batch, else {"reason": <fixed unknown reason>}.
    Timestamps, models, session membership and events.jsonl windows are
    never consulted, and ancestors beyond the direct parent are not walked."""
    parent_key = _trace_span_key(mc.get("trace_id"), mc.get("parent_span_id"))
    if parent_key is None:
        return {"reason": ATTRIBUTION_REASON_NO_SUPPORTED_LINK}
    if parent_key not in span_index:
        return {"reason": ATTRIBUTION_REASON_PARENT_NOT_OBSERVED}
    agent_info = span_index[parent_key]
    if agent_info is None:
        return {"reason": ATTRIBUTION_REASON_PARENT_NOT_AGENT}
    return {"agent_key": agent_info["agent_key"], "agent_type": agent_info["agent_type"]}


def attribution_reason_for_call(mc):
    """Unknown-bucket reason for model call `mc`, or None when it is linked
    to an agent by explicit trace context (see resolve_call_attribution).
    A call never resolved (e.g. built by an older code path) has no trace
    link and returns the "no trace parent" reason."""
    resolved = mc.get("attribution")
    if not isinstance(resolved, dict):
        resolved = resolve_call_attribution(mc, {})
    return resolved.get("reason")


def blank_attributed_agent():
    """Per-agent attributed bucket (schema v2)."""
    return {
        "agent_type": None,
        "usage": blank_agg(),
        "models": {},
        "invocation_spans": 0,
        "official_cost": {"priced_calls": 0, "usd": 0.0, "unpriced_calls": 0},
    }


# --------------------------------------------------------------------------
# Delta building / merging into the per-task aggregate
# --------------------------------------------------------------------------

def build_delta(session_id, sess_state, events_path, pricing_ctx=None):
    """`pricing_ctx` is a load_official_pricing_context() result (the
    official snapshot in effect for THIS command). If omitted, the cached
    snapshot is loaded WITHOUT any network refresh."""
    marker = get_install_marker()
    install_epoch = marker["installed_epoch"]
    if pricing_ctx is None:
        pricing_ctx = load_official_pricing_context()

    delta = {
        "by_model": {},
        "by_effort": {},
        # Joint model x effort-key aggregation ({model: {effort_key: agg}}),
        # needed for per-(model, effort) fixed request charges: by_model and
        # by_effort alone cannot be cross-multiplied back into joint counts.
        "by_model_effort": {},
        "agent_summaries": [],
        "repositories": set(),
        "min_ts": None,
        "max_ts": None,
        "model_calls": 0,
        "nano_aiu_delta": 0,
        "premium_requests_delta": 0,
        # Attribution delta: per-agent aggregates for calls linked by
        # explicit trace context, per-reason aggregates for calls that could
        # not be linked (exclusive; every call lands in exactly one agent or
        # reason) and observed invocation-registry events.
        "attribution": {"unknown": {}, "attributed": {}, "invocation_events": []},
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
        delta["attribution"]["invocation_events"].extend(ev_result["invocation_events"])

    # 2) OTEL: primary token/model/time source — independently ingestible,
    #    gated to install_epoch regardless of events.jsonl's state.
    calls, min_ts, max_ts, agent_spans = scan_otel_files(session_id, sess_state, install_epoch)
    # Per-call official cost estimate, computed NOW (at ingestion) with the
    # snapshot in effect for this command and recorded additively — a later
    # price refresh can never reprice these calls.
    delta["official_cost"] = build_official_cost_delta(calls, pricing_ctx)
    for mc in calls:
        model = mc["model"]
        effort_key = mc["effort_key"]
        delta["by_model"].setdefault(model, blank_agg())
        add_agg(delta["by_model"][model], mc)
        delta["by_effort"].setdefault(effort_key, blank_agg())
        add_agg(delta["by_effort"][effort_key], mc)
        joint = delta["by_model_effort"].setdefault(model, {})
        joint.setdefault(effort_key, blank_agg())
        add_agg(joint[effort_key], mc)
        reason = attribution_reason_for_call(mc)
        if reason is not None:
            delta["attribution"]["unknown"].setdefault(reason, blank_agg())
            add_agg(delta["attribution"]["unknown"][reason], mc)
        else:
            resolved = mc["attribution"]
            dst = _attributed_agent_entry(delta["attribution"]["attributed"], resolved)
            add_agg(dst["usage"], mc)
            dst["models"][model] = dst["models"].get(model, 0) + 1
            # Per-call official estimate recorded by build_official_cost_delta
            # above (same snapshot, same call) — summed, never re-priced.
            usd = mc.get("official_usd")
            if isinstance(usd, (int, float)) and not isinstance(usd, bool):
                dst["official_cost"]["priced_calls"] += 1
                dst["official_cost"]["usd"] += float(usd)
            else:
                dst["official_cost"]["unpriced_calls"] += 1
        delta["model_calls"] += 1
    for info in agent_spans:
        _attributed_agent_entry(delta["attribution"]["attributed"], info)["invocation_spans"] += 1
    # Feature-usage call ledger delta: one immutable entry per call with a
    # stable (session, trace, own span) key, plus explicit buckets for calls
    # that have no stable key. Purely additive; the aggregates above are
    # unchanged.
    delta["feature_ledger"] = build_feature_ledger_delta(session_id, calls, agent_spans)

    delta["min_ts"] = min_ts
    delta["max_ts"] = max_ts

    delta["repositories"] = sorted(delta["repositories"])
    return delta


def _attributed_agent_entry(attributed, info):
    """Get/create the per-agent bucket for `info` ({agent_key, agent_type}).
    A differing agent type for the same key is recorded as "mixed"."""
    dst = attributed.setdefault(info["agent_key"], blank_attributed_agent())
    agent_type = info.get("agent_type")
    if agent_type:
        if dst.get("agent_type") is None:
            dst["agent_type"] = agent_type
        elif dst["agent_type"] != agent_type:
            dst["agent_type"] = "mixed"
    return dst


def merge_delta_into_task(task, delta, session_id, task_id):
    # Defensive: convert any lingering pre-2.0 `jira_key` field to
    # `task_id` on write, even outside the startup migration path (e.g. a
    # task JSON that was hand-copied into the new location).
    if "jira_key" in task:
        task.setdefault("task_id", task.pop("jira_key"))
        task.pop("jira_key", None)
    task.setdefault("task_id", task_id)
    task.setdefault("created_at", now_iso())
    task["updated_at"] = now_iso()

    sessions = set(task.get("sessions", []))
    sessions.add(session_id)
    task["sessions"] = sorted(sessions)

    repos = set(task.get("repositories", []))
    repos.update(delta["repositories"])
    task["repositories"] = sorted(repos)

    # Must run BEFORE this delta touches task["totals"]: when the block is
    # started on an existing task, its "legacy" snapshot is exactly the usage
    # aggregated before attribution tracking existed for that task.
    attribution = prepare_attribution_block(task)
    # Same rule for the feature-usage call ledger: its pre-ledger baseline
    # (usage totals + recorded official-cost coverage) is frozen BEFORE this
    # delta's usage and cost are merged, and is never split or backfilled.
    feature_usage = prepare_feature_usage_block(task)

    totals = task.setdefault("totals", blank_agg())
    totals.setdefault("first_call_ts", None)
    totals.setdefault("last_call_ts", None)
    totals.setdefault("nano_aiu", 0)
    totals.setdefault("premium_requests", 0)

    by_model = task.setdefault("by_model", {})
    for model, agg in delta["by_model"].items():
        dst = by_model.setdefault(model, blank_agg())
        for k in blank_agg():
            dst[k] += agg[k]
            totals[k] = totals.get(k, 0) + agg[k]

    by_effort = task.setdefault("by_effort", {})
    for key, agg in delta["by_effort"].items():
        dst = by_effort.setdefault(key, blank_agg())
        for k in blank_agg():
            dst[k] += agg[k]

    # Joint model x effort aggregation. Task files persisted before this
    # field existed simply lack it: we start it empty and record when joint
    # tracking began, and NEVER backfill/guess a joint split for earlier
    # calls (the report shows those as explicitly unattributed requests —
    # see joint_unattributed_calls()). `.get(..., {})` also tolerates a
    # delta built by an older code path that lacks the key.
    delta_joint = delta.get("by_model_effort", {})
    if "by_model_effort" not in task:
        task["by_model_effort"] = {}
        task["by_model_effort_since"] = now_iso()
    by_model_effort = task["by_model_effort"]
    for model, per_effort in delta_joint.items():
        model_dst = by_model_effort.setdefault(model, {})
        for key, agg in per_effort.items():
            dst = model_dst.setdefault(key, blank_agg())
            for k in blank_agg():
                dst[k] += agg[k]

    by_agent = task.setdefault("by_agent", {})
    for summary in delta["agent_summaries"]:
        dst = by_agent.setdefault(summary["name"], {"completions": 0, "total_tokens": 0, "duration_ms": 0})
        dst["completions"] += 1
        dst["total_tokens"] += summary["total_tokens"]
        dst["duration_ms"] += summary["duration_ms"]

    # True calendar span = earliest ever call timestamp -> latest ever call
    # timestamp across all ingests/sessions for this task. NEVER sum
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

    # Official per-token cost estimates recorded at ingestion. A task file
    # persisted before this existed gets the block started now; its earlier
    # calls stay explicitly "legacy" (token counts kept, never repriced or
    # backfilled). `.get` tolerates a delta from an older code path.
    merge_official_cost_delta(task, delta.get("official_cost"))
    att_delta = delta.get("attribution")
    if att_delta is None:
        # Delta from an older code path without attribution data: its calls
        # were never evaluated for an agent link, so book them as unknown
        # under a neutral reason that makes no claim about their trace
        # context (keeps the buckets reconciled with the totals above).
        fallback = blank_agg()
        for agg in delta["by_model"].values():
            for k in blank_agg():
                fallback[k] += agg.get(k, 0)
        att_delta = {"unknown": {ATTRIBUTION_REASON_DELTA_WITHOUT_ATTRIBUTION: fallback}
                     if fallback["call_count"] else {},
                     "attributed": {}, "invocation_events": []}
    merge_attribution_delta(attribution, att_delta, session_id)
    ledger_delta = delta.get("feature_ledger")
    if ledger_delta is None:
        # Older code path without ledger data: book its calls as unledgered
        # (no stable keys are known, none are invented) so the ledger still
        # reconciles with the totals and recorded cost merged above.
        ledger_delta = _fallback_feature_ledger_delta(delta)
    merge_feature_ledger_delta(feature_usage, ledger_delta)
    return task


def _attribution_block_shape_ok(att):
    """True for a structurally valid v1 or v2 attribution block."""
    if not (isinstance(att, dict)
            and att.get("schema_version") in ATTRIBUTION_SUPPORTED_SCHEMA_VERSIONS
            and isinstance(att.get("legacy"), dict)
            and isinstance(att.get("unknown"), dict)
            and isinstance(att.get("observed_invocations"), dict)):
        return False
    if att["schema_version"] >= 2:
        return isinstance(att.get("attributed"), dict)
    return True


def prepare_attribution_block(task):
    """Return the task's current-version attribution block, starting it if
    absent.

    A newly started block freezes the task's CURRENT totals as "legacy" (all
    zero for a task with no recorded usage yet). A valid v1 block is upgraded
    in place to v2 by adding an empty "attributed" bucket (lossless: v1 never
    linked a call; its unknown rows are kept verbatim). An existing block of
    an unsupported shape/version is left untouched (never rewritten or
    "upgraded" by guessing) and None is returned, with a warning; the report
    then shows attribution as unreadable instead of a fabricated split."""
    att = task.get("attribution")
    if att is None:
        prior = task.get("totals") if isinstance(task.get("totals"), dict) else {}
        legacy = {}
        for k in blank_agg():
            value = prior.get(k, 0)
            legacy[k] = value if isinstance(value, int) and not isinstance(value, bool) else 0
        since = now_iso()
        att = task["attribution"] = {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "since": since,
            "attributed_since": since,
            "legacy": legacy,
            "unknown": {},
            "attributed": {},
            "observed_invocations": {},
            "provenance": dict(ATTRIBUTION_PROVENANCE),
        }
        return att
    if _attribution_block_shape_ok(att):
        if att["schema_version"] == 1:
            att["schema_version"] = ATTRIBUTION_SCHEMA_VERSION
            att["upgraded_from_schema_version"] = 1
            att["attributed_since"] = now_iso()
            att["attributed"] = {}
            provenance = att.get("provenance")
            if isinstance(provenance, dict):
                provenance.setdefault("attributed", ATTRIBUTION_PROVENANCE["attributed"])
            else:
                att["provenance"] = dict(ATTRIBUTION_PROVENANCE)
        return att
    print("Warning: task '%s' has an unsupported/malformed attribution block; leaving it unchanged "
          "(new calls are still counted in the task totals)." % task.get("task_id", "?"), file=sys.stderr)
    return None


def merge_attribution_delta(att, att_delta, session_id):
    """Merge one ingest's attribution delta into the current-version block
    (or do nothing if `att` is None — see prepare_attribution_block).

    Unknown-reason and per-agent attributed aggregates are additive. Registry merging is idempotent
    per (session id, agentId, event kind): replaying an identical observation
    is a no-op; a different timestamp/model for an already-recorded event
    keeps the first value and increments `conflicting_observations` instead
    of silently overwriting it."""
    if att is None or not att_delta:
        return
    unknown = att["unknown"]
    for reason, agg in (att_delta.get("unknown") or {}).items():
        dst = unknown.setdefault(reason, blank_agg())
        for k in blank_agg():
            dst[k] = dst.get(k, 0) + agg.get(k, 0)

    attributed = att.get("attributed")
    for key, src in (att_delta.get("attributed") or {}).items():
        if not isinstance(attributed, dict) or not isinstance(src, dict):
            continue
        dst = attributed.get(key)
        if not isinstance(dst, dict):
            dst = attributed[key] = blank_attributed_agent()
        src_type = src.get("agent_type")
        if src_type:
            if dst.get("agent_type") is None:
                dst["agent_type"] = src_type
            elif dst["agent_type"] != src_type:
                dst["agent_type"] = "mixed"
        usage = dst.setdefault("usage", blank_agg())
        for k in blank_agg():
            usage[k] = _int_or_zero(usage.get(k)) + _int_or_zero((src.get("usage") or {}).get(k))
        models = dst.setdefault("models", {})
        for model, n in (src.get("models") or {}).items():
            models[model] = _int_or_zero(models.get(model)) + _int_or_zero(n)
        dst["invocation_spans"] = _int_or_zero(dst.get("invocation_spans")) + _int_or_zero(
            src.get("invocation_spans"))
        cost = dst.setdefault("official_cost", {"priced_calls": 0, "usd": 0.0, "unpriced_calls": 0})
        src_cost = src.get("official_cost") or {}
        cost["priced_calls"] = _int_or_zero(cost.get("priced_calls")) + _int_or_zero(src_cost.get("priced_calls"))
        cost["unpriced_calls"] = _int_or_zero(cost.get("unpriced_calls")) + _int_or_zero(
            src_cost.get("unpriced_calls"))
        cost["usd"] = _float_or_zero(cost.get("usd")) + _float_or_zero(src_cost.get("usd"))

    events = att_delta.get("invocation_events") or []
    if not events:
        return
    registry = att["observed_invocations"].setdefault(session_id, {})
    for ev in events:
        agent_id = ev.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        name = ev.get("agent_name") if isinstance(ev.get("agent_name"), str) else "unknown-agent"
        entry = registry.get(agent_id)
        if not isinstance(entry, dict):
            entry = registry[agent_id] = {
                "agent_name": name,
                "started": False,
                "started_ts": None,
                "completed": False,
                "completed_ts": None,
                "model": None,
                "conflicting_observations": 0,
            }
        if entry.get("agent_name") in (None, "unknown-agent") and name != "unknown-agent":
            entry["agent_name"] = name
        elif name not in ("unknown-agent", entry.get("agent_name")):
            entry["conflicting_observations"] = entry.get("conflicting_observations", 0) + 1
        kind = "completed" if ev.get("event") == "completed" else "started"
        ts_key = "%s_ts" % kind
        if not entry.get(kind):
            entry[kind] = True
            entry[ts_key] = ev.get("ts")
        elif entry.get(ts_key) != ev.get("ts"):
            entry["conflicting_observations"] = entry.get("conflicting_observations", 0) + 1
        model = ev.get("model")
        if model:
            if entry.get("model") is None:
                entry["model"] = model
            elif entry["model"] != model:
                entry["conflicting_observations"] = entry.get("conflicting_observations", 0) + 1


def _int_or_zero(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float_or_zero(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) else 0.0


def summarize_attribution(task):
    """Read-only summary of the attribution block for rendering.

    status: "active" (v1 or v2 block; a v1 block not yet upgraded by an
    ingest simply has no attributed bucket), "not_started" (task predates
    attribution tracking and has not been re-ingested: everything is shown
    as legacy), or "unreadable" (unsupported/malformed block; nothing is
    inferred). Per-agent rows only ever come from the recorded explicit
    trace-context join; nothing is derived here."""
    totals = task.get("totals") if isinstance(task.get("totals"), dict) else {}
    total_calls = _int_or_zero(totals.get("call_count"))
    total_tokens = _int_or_zero(totals.get("total_tokens"))
    att = task.get("attribution")
    out = {
        "status": "active",
        "since": None,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "legacy_calls": 0,
        "legacy_tokens": 0,
        "unknown": {},
        "unknown_calls": 0,
        "unknown_tokens": 0,
        "attributed_calls": 0,
        "attributed_tokens": 0,
        "schema_version": None,
        "attributed_since": None,
        "agents": {},
        "attributed_priced_calls": 0,
        "attributed_unpriced_calls": 0,
        "attributed_usd": 0.0,
        "invocation_spans": 0,
        "agents_without_attributed_calls": 0,
        "v1_unknown_calls": 0,
        "reconciled": True,
        "registry": {},
        "registry_sessions": 0,
        "registry_invocations": 0,
        "registry_conflicts": 0,
    }
    if att is None:
        out["status"] = "not_started"
        out["legacy_calls"] = total_calls
        out["legacy_tokens"] = total_tokens
        return out
    if not _attribution_block_shape_ok(att):
        out["status"] = "unreadable"
        out["reconciled"] = False
        return out
    out["since"] = att.get("since")
    out["schema_version"] = att["schema_version"]
    out["attributed_since"] = att.get("attributed_since") if att["schema_version"] >= 2 else None
    out["legacy_calls"] = _int_or_zero(att["legacy"].get("call_count"))
    out["legacy_tokens"] = _int_or_zero(att["legacy"].get("total_tokens"))
    for reason, agg in att["unknown"].items():
        if not isinstance(agg, dict):
            continue
        calls = _int_or_zero(agg.get("call_count"))
        tokens = _int_or_zero(agg.get("total_tokens"))
        out["unknown"][reason] = {"calls": calls, "total_tokens": tokens}
        out["unknown_calls"] += calls
        out["unknown_tokens"] += tokens
        if reason == ATTRIBUTION_REASON_V1_NO_LINK:
            out["v1_unknown_calls"] += calls
    for key, entry in (att.get("attributed") or {}).items():
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        cost = entry.get("official_cost") if isinstance(entry.get("official_cost"), dict) else {}
        models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
        row = {
            "agent_type": entry.get("agent_type") if isinstance(entry.get("agent_type"), str) else None,
            "calls": _int_or_zero(usage.get("call_count")),
            "prompt_tokens": _int_or_zero(usage.get("prompt_tokens")),
            "completion_tokens": _int_or_zero(usage.get("completion_tokens")),
            "reasoning_tokens": _int_or_zero(usage.get("reasoning_tokens")),
            "total_tokens": _int_or_zero(usage.get("total_tokens")),
            "duration_ms": _int_or_zero(usage.get("duration_ms")),
            "models": {m: _int_or_zero(n) for m, n in models.items() if _int_or_zero(n)},
            "invocation_spans": _int_or_zero(entry.get("invocation_spans")),
            "priced_calls": _int_or_zero(cost.get("priced_calls")),
            "unpriced_calls": _int_or_zero(cost.get("unpriced_calls")),
            "usd": _float_or_zero(cost.get("usd")),
        }
        out["agents"][key] = row
        out["attributed_calls"] += row["calls"]
        out["attributed_tokens"] += row["total_tokens"]
        out["attributed_priced_calls"] += row["priced_calls"]
        out["attributed_unpriced_calls"] += row["unpriced_calls"]
        out["attributed_usd"] += row["usd"]
        out["invocation_spans"] += row["invocation_spans"]
        if row["invocation_spans"] and not row["calls"]:
            out["agents_without_attributed_calls"] += 1
    out["reconciled"] = (
        out["legacy_calls"] + out["unknown_calls"] + out["attributed_calls"] == total_calls
        and out["legacy_tokens"] + out["unknown_tokens"] + out["attributed_tokens"] == total_tokens
    )
    for sid, per_session in att["observed_invocations"].items():
        if not isinstance(per_session, dict):
            continue
        counted = False
        for entry in per_session.values():
            if not isinstance(entry, dict):
                continue
            counted = True
            name = entry.get("agent_name") if isinstance(entry.get("agent_name"), str) else "unknown-agent"
            row = out["registry"].setdefault(name, {"invocations": 0, "completed": 0, "started_only": 0,
                                                    "completed_only": 0, "conflicts": 0})
            row["invocations"] += 1
            started, completed = bool(entry.get("started")), bool(entry.get("completed"))
            if completed:
                row["completed"] += 1
            if started and not completed:
                row["started_only"] += 1
            if completed and not started:
                row["completed_only"] += 1
            conflicts = _int_or_zero(entry.get("conflicting_observations"))
            row["conflicts"] += conflicts
            out["registry_conflicts"] += conflicts
            out["registry_invocations"] += 1
        if counted:
            out["registry_sessions"] += 1
    return out


def observed_invocation_status(task, session_id, invocation_id):
    """Exact-identifier lookup of a recorded review's invocation in the
    task's observed registry. Never infers a session or a link."""
    if invocation_id == REVIEW_UNKNOWN:
        return "explicitly unknown"
    if session_id == REVIEW_UNKNOWN:
        return "not verifiable (session unknown)"
    att = task.get("attribution")
    registry = att.get("observed_invocations") if isinstance(att, dict) else None
    per_session = registry.get(session_id) if isinstance(registry, dict) else None
    if isinstance(per_session, dict) and isinstance(per_session.get(invocation_id), dict):
        return "observed in events.jsonl registry"
    return "not observed in this task's registry (not ingested yet, or recorded under another task)"


# --------------------------------------------------------------------------
# Official GitHub Copilot per-token pricing (automatic, cached snapshot)
# --------------------------------------------------------------------------
#
# The estimated USD cost uses GitHub's OFFICIAL published per-token rates
# ("Models and pricing for GitHub Copilot"), fetched automatically from the
# docs article-body API, validated, and cached under SUPPORT_DIR for 24h.
#
# - Refresh happens at most once per `ingest`/`report` command (never on
#   import, render, ensure-marker or normalize-task-id), only when the cache
#   is missing/invalid/older than the TTL, under a dedicated file lock, with
#   a bounded response size and best-effort time limits that are NOT a hard
#   deadline: each blocking socket operation (connect, each receive) has a
#   10s timeout and the 20s total deadline is only checked between body
#   reads, so a server streaming headers or body slowly, slow DNS
#   resolution (not covered by the socket timeout) or a wait for the
#   pricing lock can make an attempt exceed the nominal timeout. After a failed
#   attempt, further attempts are suppressed for
#   OFFICIAL_PRICING_RETRY_BACKOFF_SECONDS (1h), so while the source keeps
#   failing a refresh is retried at most hourly (not once per 24h) and every
#   incremental ingest isn't slowed by a dead network.
# - A failed fetch/parse NEVER replaces the last valid cache: it is kept and
#   used (labeled stale in the report), and a warning goes to stderr — but
#   only up to OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS (7 days) after it
#   was FETCHED. A cached snapshot older than that (or dated in the future)
#   is kept for reference only: calls ingested while it is the only snapshot
#   are recorded as unpriced with the fixed reason UNPRICED_SNAPSHOT_TOO_OLD.
#   `fetched_at` is when this tool downloaded the page, NOT a date from
#   which GitHub's rates were effective (the page publishes none).
# - A corrupted refresh-state file (invalid UTF-8/JSON, wrong types, negative
#   or non-finite counters) is reset with a warning; it only holds retry
#   metadata, so the last valid snapshot and recorded task costs are kept.
# - The parser is strict: every pricing table must have a recognized
#   schema, every row a valid $ amount for input/cached input/output, and
#   tier thresholds must be "Not applicable" or a matching "≤ N K"/"> N K"
#   Default/Long-context pair. Anything else rejects the WHOLE refresh (no
#   partially parsed snapshot is ever cached). No rate is ever hardcoded or
#   guessed; models absent from the official tables are unpriced.
# - Costs are computed PER CALL at ingestion and recorded additively in the
#   task JSON under the snapshot id that priced them, so a later refresh
#   never reprices past estimates. Delayed ingestion uses the tariffs
#   observed at ingestion time, which are not necessarily the rates in
#   force when the call happened (labeled as such in the report).
# - Usage semantics (verified against real Copilot CLI OTEL spans):
#   `cache_read.input_tokens` and `cache_write.input_tokens` are SUBSETS of
#   `input_tokens` (their sum never exceeded input across ~3,000 spans, for
#   OpenAI, Anthropic, Google and Moonshot models alike); the CLI omits
#   zero-valued cache attributes (it never emits 0), so an absent cache
#   attribute means 0. Absent input/output counts are never treated as 0 —
#   the call is unpriced. Reasoning tokens are a subset of output and are
#   never priced a second time; a call whose reasoning count exceeds its
#   output count (seen for some Gemini spans, where output may EXCLUDE
#   reasoning) is unpriced rather than guessed. Because that exclusion
#   cannot be detected when reasoning <= output, EVERY call to a Gemini /
#   Google-provider model that reports reasoning_tokens > 0 is
#   conservatively unpriced (UNPRICED_GEMINI_REASONING); its token counts
#   are still recorded unchanged and nothing is estimated for it.

OFFICIAL_PRICING_API_URL = (
    "https://docs.github.com/api/article/body?pathname="
    "/en/copilot/reference/copilot-billing/models-and-pricing"
)
OFFICIAL_PRICING_PUBLIC_URL = (
    "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
)
OFFICIAL_PRICING_ALLOWED_URL_PREFIX = "https://docs.github.com/"
OFFICIAL_PRICING_CACHE_FILENAME = "official-pricing-cache.json"
OFFICIAL_PRICING_STATE_FILENAME = "official-pricing-refresh-state.json"
OFFICIAL_PRICING_LOCK_FILENAME = ".official-pricing.lock"
OFFICIAL_PRICING_TTL_SECONDS = 24 * 3600
# A cached snapshot fetched longer ago than this (by its `fetched_epoch`,
# i.e. download time — not a rate effective date) is never used to price
# newly ingested calls; it is kept on disk and shown for reference only.
OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS = 7 * 24 * 3600
OFFICIAL_PRICING_RETRY_BACKOFF_SECONDS = 3600
# Explicit, user-requested refreshes (refresh_official_pricing_now, used by
# copilot-model-refresh.py / copilot-s menu `p`) bypass the 24h TTL and the
# failure backoff, but still reuse a snapshot fetched less than this long ago
# instead of re-downloading it (repeated menu presses never hammer the docs).
OFFICIAL_PRICING_MANUAL_MIN_INTERVAL_SECONDS = 300
OFFICIAL_PRICING_TIMEOUT_SECONDS = 10
OFFICIAL_PRICING_TOTAL_DEADLINE_SECONDS = 20
OFFICIAL_PRICING_MAX_BYTES = 1_000_000
OFFICIAL_PRICING_SCHEMA_VERSION = 1
OFFICIAL_PRICING_KIND = "github-copilot-official-model-pricing"
# Set to "0"/"off"/"false"/"no" to disable all network fetching (the cached
# snapshot, if any, is still used). Intended for offline use and hermetic
# test fixtures.
OFFICIAL_PRICING_FETCH_ENV = "COPILOT_TASK_REPORT_PRICING_FETCH"
OFFICIAL_COST_SCHEMA_VERSION = 1
# Injectable transport for tests/offline fixtures: a callable
# (url, timeout_seconds, max_bytes) -> bytes. None = stdlib urllib fetch.
PRICING_FETCHER = None

# Per-process memo: cache path -> refresh result, so a command refreshes at
# most once no matter how many times it asks.
_OFFICIAL_REFRESH_RESULTS = {}

TIER_DEFAULT = "default"
TIER_LONG = "long_context"
TIER_LABELS = {TIER_DEFAULT: "Default", TIER_LONG: "Long context"}

CACHE_WRITE_RATE = "rate"
CACHE_WRITE_NOT_APPLICABLE = "not_applicable"  # official "Not applicable": no separate cache-write cost
CACHE_WRITE_NOT_LISTED = "not_listed"  # provider table has no cache-write column at all

UNPRICED_NO_SNAPSHOT = "no valid official pricing snapshot was available at ingestion"
# Fixed text (no ages/dates interpolated) so all such calls share ONE
# recorded unpriced bucket per model.
UNPRICED_SNAPSHOT_TOO_OLD = ("the only cached official pricing snapshot was fetched more than 7 days before "
                             "ingestion (or is dated in the future); kept for reference only, not used for "
                             "pricing (its fetch time is not a rate effective date)")
UNPRICED_GEMINI_REASONING = ("Gemini/Google-provider call with reasoning tokens: reported output may exclude "
                             "reasoning, so billable output is uncertain (conservatively unpriced; token "
                             "counts kept)")
UNPRICED_UNKNOWN_MODEL = "model not listed in the official GitHub pricing snapshot used at ingestion"
UNPRICED_NO_INPUT = "input token count missing from telemetry (never assumed 0)"
UNPRICED_NO_OUTPUT = "output token count missing from telemetry (never assumed 0)"
UNPRICED_NEGATIVE = "negative token count in telemetry"
UNPRICED_INVALID_USAGE = ("a usage count in telemetry is not a valid count (boolean, numeric string, fraction, "
                          "negative, unsafe huge, NaN/Infinity or non-numeric); the converted value is not "
                          "trusted, never priced and never assumed 0")
UNPRICED_CACHE_EXCEEDS_INPUT = "cache-read + cache-write tokens exceed input tokens (unexpected usage shape)"
UNPRICED_REASONING_EXCEEDS_OUTPUT = ("reasoning tokens exceed output tokens (output may exclude reasoning; "
                                     "billable output unknown)")
UNPRICED_TIER_AMBIGUOUS = ("input tokens fall between the tier threshold read as K=1,000 and K=1,024 "
                           "(tier ambiguous)")
UNPRICED_CACHE_WRITE_NOT_LISTED = ("cache-write tokens reported but the official table lists no cache-write "
                                   "rate for this model")


class OfficialPricingError(ValueError):
    """Raised when the official pricing source or a cached snapshot is
    missing, malformed, or uses an unsupported schema."""


def official_pricing_cache_path():
    return os.path.join(SUPPORT_DIR, OFFICIAL_PRICING_CACHE_FILENAME)


def official_pricing_state_path():
    return os.path.join(SUPPORT_DIR, OFFICIAL_PRICING_STATE_FILENAME)


def official_pricing_fetch_enabled():
    val = os.environ.get(OFFICIAL_PRICING_FETCH_ENV, "").strip().lower()
    return val not in ("0", "off", "false", "no")


def _iso_from_epoch(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def official_pricing_lock():
    """Dedicated cross-process lock for the cache refresh (kept separate from
    the ingest lock so a slow network never holds up task-file writes)."""
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    if fcntl is None:
        yield
        return
    fd = os.open(os.path.join(SUPPORT_DIR, OFFICIAL_PRICING_LOCK_FILENAME), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _default_pricing_fetch(url, timeout, max_bytes):
    """Bounded stdlib HTTPS GET: per-socket-operation timeout, a total
    deadline checked between reads (not a hard wall-clock limit; DNS
    resolution is not covered by the socket timeout), size cap, and the
    final URL must stay on docs.github.com over HTTPS. Transport/protocol
    errors surface as OSError (incl. urllib.error.URLError, timeouts, TLS)
    or OfficialPricingError (http.client protocol errors are wrapped).
    A Python TLS certificate-verification failure (and only that) retries
    once via the system curl with verification still ON — see
    _curl_pricing_fetch; its failures surface as OfficialPricingError."""
    import http.client  # lazy: never imported unless a refresh is due
    import urllib.request

    try:
        return _default_pricing_fetch_inner(urllib.request, url, timeout, max_bytes)
    except http.client.HTTPException as ex:
        raise OfficialPricingError("HTTP protocol error: %s: %s" % (type(ex).__name__, ex))
    except OSError as ex:
        # ONLY a Python certificate-verification failure (typically a
        # python.org build without its CA bundle installed) falls back to
        # the system curl, which still verifies the certificate. Every other
        # network/HTTP/timeout/parse error propagates unchanged.
        if not _is_tls_cert_verification_error(ex):
            raise
        python_error = ("%s: %s" % (type(ex).__name__, ex))[:200]
        body = _curl_pricing_fetch(url, timeout, max_bytes, python_error)
        print("Note: Python could not verify the TLS certificate for docs.github.com (%s); fetched the "
              "official pricing with the system curl instead (HTTPS only, certificate verification ON). "
              "Repair Python's CA certificates to silence this note." % python_error, file=sys.stderr)
        return body


# System-curl fallback (used only after a Python TLS certificate-verification
# failure; see _default_pricing_fetch). Fixed argv, never a shell.
_CURL_STATUS_MARKER = "\n@@copilot-task-report-curl-status@@ "
_CURL_CANDIDATE_PATHS = ("/usr/bin/curl",)


def _is_tls_cert_verification_error(ex):
    """True only for ssl.SSLCertVerificationError, raw or wrapped as the
    `reason` of urllib.error.URLError (never for HTTPError or other errors)."""
    try:
        import ssl
    except ImportError:
        return False
    cert_error = getattr(ssl, "SSLCertVerificationError", None)
    if cert_error is None:
        return False
    if isinstance(ex, cert_error):
        return True
    import urllib.error
    if isinstance(ex, urllib.error.HTTPError):
        return False
    return isinstance(ex, urllib.error.URLError) and isinstance(getattr(ex, "reason", None), cert_error)


def _find_system_curl():
    for path in _CURL_CANDIDATE_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("curl")


def _curl_pricing_fetch(url, timeout, max_bytes, python_error):
    """Bounded HTTPS GET of the FIXED official pricing URL via the system
    curl, with certificate verification left ON (never -k/--insecure).
    `-q` must stay first so no ~/.curlrc can inject options (e.g. -k). No
    redirects are followed (no -L); only HTTP 200 from the same URL is
    accepted. Output is read in chunks and capped at max_bytes; curl's
    --max-time and a subprocess wait timeout bound the wall-clock time.
    Raises OfficialPricingError (a ValueError) on any failure."""
    import subprocess

    if url != OFFICIAL_PRICING_API_URL:
        raise OfficialPricingError("system curl fallback refused: only the fixed official pricing URL is allowed")
    curl = _find_system_curl()
    if not curl:
        raise OfficialPricingError(
            "Python could not verify the TLS certificate for docs.github.com (%s) and no system curl was "
            "found for the verified fallback; repair Python's CA certificates (python.org builds on macOS: run "
            "'Install Certificates.command'; otherwise point SSL_CERT_FILE at a valid CA bundle)" % python_error)
    max_time = OFFICIAL_PRICING_TOTAL_DEADLINE_SECONDS
    argv = [
        curl, "-q",
        "--fail", "--silent", "--show-error",
        "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
        "--connect-timeout", str(int(timeout)),
        "--max-time", str(int(max_time)),
        "--max-filesize", str(int(max_bytes)),
        "--header", "User-Agent: copilot-task-report (copilot-cli-toolkit official pricing refresh)",
        "--header", "Accept: text/markdown, text/plain;q=0.9, */*;q=0.1",
        "--write-out", _CURL_STATUS_MARKER + "%{http_code} %{url_effective}",
        "--url", url,
    ]
    # Room for the status trailer (marker + 3-digit code + the fixed URL).
    output_cap = max_bytes + len(_CURL_STATUS_MARKER) + len(url) + 16
    chunks = []
    size = 0
    try:
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=err_file, shell=False, close_fds=True)
            try:
                while True:
                    chunk = proc.stdout.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > output_cap:
                        raise OfficialPricingError("system curl fallback: response too large (> %d bytes)"
                                                   % max_bytes)
                    chunks.append(chunk)
                returncode = proc.wait(timeout=max_time + 5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                proc.stdout.close()
            err_file.seek(0)
            err_text = err_file.read(2000).decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        raise OfficialPricingError("system curl fallback exceeded %ds" % (max_time + 5))
    except (OSError, subprocess.SubprocessError) as ex:
        raise OfficialPricingError("system curl fallback could not run (%s: %s); Python TLS error was: %s"
                                   % (type(ex).__name__, ex, python_error))
    if returncode != 0:
        raise OfficialPricingError("system curl fallback failed (exit %d: %s); Python TLS error was: %s"
                                   % (returncode, err_text[:200] or "no message", python_error))
    out = b"".join(chunks)
    marker = _CURL_STATUS_MARKER.encode("ascii")
    idx = out.rfind(marker)
    if idx < 0:
        raise OfficialPricingError("system curl fallback: missing HTTP status trailer")
    body, trailer = out[:idx], out[idx + len(marker):].decode("ascii", "replace").strip()
    status, _, final_url = trailer.partition(" ")
    if status != "200":
        raise OfficialPricingError("system curl fallback: unexpected HTTP status %s" % (status or "?"))
    if final_url != url or not final_url.startswith(OFFICIAL_PRICING_ALLOWED_URL_PREFIX):
        raise OfficialPricingError("system curl fallback: unexpected final URL")
    if len(body) > max_bytes:
        raise OfficialPricingError("system curl fallback: response too large (> %d bytes)" % max_bytes)
    return body


def _default_pricing_fetch_inner(urllib_request, url, timeout, max_bytes):
    req = urllib_request.Request(url, headers={
        "User-Agent": "copilot-task-report (copilot-cli-toolkit official pricing refresh)",
        "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1",
    })
    deadline = time.monotonic() + OFFICIAL_PRICING_TOTAL_DEADLINE_SECONDS
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if status != 200:
            raise OfficialPricingError("unexpected HTTP status %s" % status)
        final_url = resp.geturl() or url
        if not final_url.startswith(OFFICIAL_PRICING_ALLOWED_URL_PREFIX):
            raise OfficialPricingError("redirected away from %s" % OFFICIAL_PRICING_ALLOWED_URL_PREFIX)
        clen = resp.headers.get("Content-Length")
        if clen and clen.strip().isdigit() and int(clen) > max_bytes:
            raise OfficialPricingError("response too large (%s bytes > %d)" % (clen.strip(), max_bytes))
        chunks = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise OfficialPricingError("download exceeded %ds total deadline" % OFFICIAL_PRICING_TOTAL_DEADLINE_SECONDS)
            chunk = resp.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise OfficialPricingError("response too large (> %d bytes)" % max_bytes)
            chunks.append(chunk)
    return b"".join(chunks)


# ---- Parsing the official Markdown article body ----

_PRICE_CELL_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_THRESHOLD_CELL_RE = re.compile(r"^(≤|<=|>)\s*(\d+)\s*([Kk])$")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:\s*(\S.*)$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*[a-z0-9]$")
_NOT_APPLICABLE = "not applicable"

_OFFICIAL_COLUMNS = {
    "model": "model",
    # Descriptive (non-price) columns: recorded per model when present and
    # consistent across its tier rows; never used for pricing.
    "release status": "release_status",
    "category": "category",
    "tier": "tier",
    "threshold (input tokens)": "threshold",
    "input": "input",
    "cached input": "cached_input",
    "cache write": "cache_write",
    "output": "output",
}
_REQUIRED_COLUMNS = ("model", "input", "cached_input", "output")
_OFFICIAL_LABEL_MAX_LEN = 80
_OFFICIAL_LABEL_KEYS = ("category", "release_status")
_IMAGE_GENERATION_HEADING = "image generation models"
_IMAGE_GENERATION_COLUMNS = (
    "model", "release status", "text input", "cached text input",
    "image input", "cached image input", "image output",
)


def _split_table_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _clean_cell_text(text):
    text = text.replace("\\", "")
    text = re.sub(r"[*`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_official_model_name(display_name):
    """Official display name -> model id (e.g. 'GPT-5.4 mini' -> 'gpt-5.4-mini',
    'Claude Opus 4.8 (fast mode) (preview)' -> 'claude-opus-4.8-fast-mode-preview').
    Purely mechanical (lowercase, spaces/parentheses -> '-'); never guesses."""
    s = display_name.lower()
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not _MODEL_ID_RE.match(s):
        raise OfficialPricingError("cannot derive a model id from official model name %r" % display_name)
    return s


def official_lookup_key(model_id):
    """Loose key used ONLY to accept the dash-for-dot spelling of a version
    number ('claude-opus-4-8' == 'claude-opus-4.8'). Validated per snapshot:
    a key shared by two different official models is dropped (exact match
    only for those)."""
    return re.sub(r"(?<=\d)\.(?=\d)", "-", model_id.strip().lower())


def _parse_price_cell(cell, where):
    m = _PRICE_CELL_RE.match(cell)
    if not m:
        raise OfficialPricingError("%s: expected a $ amount, got %r" % (where, cell))
    value = float(m.group(1))
    if not math.isfinite(value) or value < 0:
        raise OfficialPricingError("%s: invalid amount %r" % (where, cell))
    return value


def _parse_threshold_cell(cell, where):
    if cell.lower() == _NOT_APPLICABLE:
        return None
    m = _THRESHOLD_CELL_RE.match(cell.replace("\u00a0", " "))
    if not m:
        raise OfficialPricingError("%s: unsupported tier threshold %r" % (where, cell))
    comparator = "<=" if m.group(1) in ("≤", "<=") else ">"
    k_value = int(m.group(2))
    if k_value <= 0:
        raise OfficialPricingError("%s: invalid tier threshold %r" % (where, cell))
    return {"comparator": comparator, "k_value": k_value}


def _parse_tier_cell(cell, where):
    norm = cell.strip().lower()
    if norm == "default":
        return TIER_DEFAULT
    if norm == "long context":
        return TIER_LONG
    raise OfficialPricingError("%s: unsupported tier %r" % (where, cell))


def parse_official_pricing_markdown(text):
    """Parse the official article body (Markdown) into
    {'models': {id: entry}, 'lookup': {loose_key: id}, 'providers': [...]}.
    Excludes the explicitly recognized, unsupported image-generation table.
    Raises OfficialPricingError on any other inconsistency — the caller
    must then keep the previous cache rather than store a partial result."""
    if not isinstance(text, str) or not text.strip():
        raise OfficialPricingError("empty pricing document")
    lines = text.splitlines()

    footnotes = {}
    for line in lines:
        m = _FOOTNOTE_DEF_RE.match(line.strip())
        if m:
            if m.group(1) in footnotes:
                raise OfficialPricingError("duplicate footnote definition [^%s]" % m.group(1))
            footnotes[m.group(1)] = _clean_cell_text(m.group(2))

    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "## pricing tables":
            start = i + 1
            break
    if start is None:
        raise OfficialPricingError("'## Pricing tables' section not found")
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    section = lines[start:end]

    prose_before_tables = []
    for line in section:
        if line.lstrip().startswith("|") or line.startswith("### "):
            break
        prose_before_tables.append(line)
    if "per 1 million tokens" not in " ".join(prose_before_tables).lower().replace("*", ""):
        raise OfficialPricingError("pricing unit statement 'per 1 million tokens' not found")

    models = {}
    providers = []
    provider = None
    i = 0
    n_tables = 0
    while i < len(section):
        line = section[i]
        if line.startswith("### "):
            provider = _clean_cell_text(line[4:])
            if not provider:
                raise OfficialPricingError("empty provider heading")
            if provider.lower() != _IMAGE_GENERATION_HEADING:
                providers.append(provider)
            i += 1
            continue
        if not line.lstrip().startswith("|"):
            i += 1
            continue
        # A table: header, separator, rows (until the first non-'|' line).
        if provider is None:
            raise OfficialPricingError("pricing table found before any provider heading")
        block = []
        while i < len(section) and section[i].lstrip().startswith("|"):
            block.append(section[i])
            i += 1
        n_tables += 1
        where_t = "provider %r table" % provider
        if len(block) < 2:
            raise OfficialPricingError("%s: missing header/separator" % where_t)
        header = [_clean_cell_text(c).lower() for c in _split_table_row(block[0])]
        sep = _split_table_row(block[1])
        if len(sep) != len(header) or not all(_TABLE_SEPARATOR_CELL_RE.match(c) for c in sep):
            raise OfficialPricingError("%s: malformed header separator" % where_t)
        # Image modalities cannot be priced with chat token usage. Match both
        # the explicit section and its known schema, never merely a missing
        # chat column: a broken supported table must still reject the refresh.
        if (provider.lower() == _IMAGE_GENERATION_HEADING
                and tuple(header) == _IMAGE_GENERATION_COLUMNS):
            continue
        col_index = {}
        unknown_cols = []
        for idx, name in enumerate(header):
            if name in _OFFICIAL_COLUMNS:
                key = _OFFICIAL_COLUMNS[name]
                if key is None:
                    continue
                if key in col_index:
                    raise OfficialPricingError("%s: duplicate column %r" % (where_t, name))
                col_index[key] = idx
            else:
                unknown_cols.append(idx)
        for req in _REQUIRED_COLUMNS:
            if req not in col_index:
                raise OfficialPricingError("%s: required column %r missing (header %r)" % (where_t, req, header))
        if ("tier" in col_index) != ("threshold" in col_index):
            raise OfficialPricingError("%s: 'Tier' and 'Threshold (input tokens)' must appear together" % where_t)

        rows_by_model = {}
        for raw_row in block[2:]:
            cells = _split_table_row(raw_row)
            if all(c == "" for c in cells):
                continue  # visual spacer rows in the source
            if len(cells) != len(header):
                raise OfficialPricingError("%s: row has %d cells, header has %d: %r"
                                           % (where_t, len(cells), len(header), raw_row.strip()))
            raw_model = cells[col_index["model"]]
            refs = _FOOTNOTE_REF_RE.findall(raw_model)
            display = _clean_cell_text(_FOOTNOTE_REF_RE.sub("", raw_model))
            if not display:
                raise OfficialPricingError("%s: empty model name" % where_t)
            model_id = normalize_official_model_name(display)
            where = "%s, model %r" % (where_t, display)
            for idx in unknown_cols:
                if "$" in cells[idx]:
                    raise OfficialPricingError("%s: unrecognized price column %r" % (where, header[idx]))
            notes = []
            for ref in refs:
                if ref not in footnotes:
                    raise OfficialPricingError("%s: footnote [^%s] has no definition" % (where, ref))
                notes.append(footnotes[ref])
            tier = _parse_tier_cell(cells[col_index["tier"]], where) if "tier" in col_index else TIER_DEFAULT
            threshold = _parse_threshold_cell(cells[col_index["threshold"]], where) if "threshold" in col_index else None
            rates = {
                "input": _parse_price_cell(cells[col_index["input"]], where + " input"),
                "cached_input": _parse_price_cell(cells[col_index["cached_input"]], where + " cached input"),
                "output": _parse_price_cell(cells[col_index["output"]], where + " output"),
            }
            if "cache_write" in col_index:
                cw_cell = cells[col_index["cache_write"]]
                if cw_cell.lower() == _NOT_APPLICABLE:
                    rates["cache_write"] = None
                    rates["cache_write_status"] = CACHE_WRITE_NOT_APPLICABLE
                else:
                    rates["cache_write"] = _parse_price_cell(cw_cell, where + " cache write")
                    rates["cache_write_status"] = CACHE_WRITE_RATE
            else:
                rates["cache_write"] = None
                rates["cache_write_status"] = CACHE_WRITE_NOT_LISTED
            rows = rows_by_model.setdefault(model_id, {"display_name": display, "rows": [], "notes": [],
                                                       "labels": {"category": set(), "release_status": set()}})
            if rows["display_name"] != display:
                raise OfficialPricingError("%s: conflicting display names for id %r" % (where, model_id))
            rows["rows"].append({"tier": tier, "threshold": threshold, "rates": rates})
            for label_key in ("category", "release_status"):
                if label_key in col_index:
                    rows["labels"][label_key].add(_clean_cell_text(cells[col_index[label_key]]))
            for note in notes:
                if note not in rows["notes"]:
                    rows["notes"].append(note)

        for model_id, info in rows_by_model.items():
            where = "%s, model %r" % (where_t, info["display_name"])
            if model_id in models:
                raise OfficialPricingError("%s: model listed more than once across tables" % where)
            tiers, threshold = _validate_official_tier_rows(info["rows"], where)
            models[model_id] = {
                "display_name": info["display_name"],
                "provider": provider,
                "notes": info["notes"],
                "threshold": threshold,
                "tiers": tiers,
            }
            # Optional descriptive labels: kept only when exactly one
            # non-empty, bounded value appears across the model's rows
            # (anything else is simply omitted — never guessed).
            for label_key, values in info["labels"].items():
                if len(values) == 1:
                    value = next(iter(values))
                    if value and len(value) <= _OFFICIAL_LABEL_MAX_LEN:
                        models[model_id][label_key] = value

    if n_tables == 0:
        raise OfficialPricingError("no pricing tables found")
    if not models:
        raise OfficialPricingError("no priced models found in the pricing tables")
    return {"models": models, "lookup": build_official_lookup(models), "providers": providers}


def _validate_official_tier_rows(rows, where):
    """Accept exactly one Default row with no threshold, or exactly a Default
    '≤ N K' row plus a Long-context '> N K' row with the same N."""
    if len(rows) == 1:
        row = rows[0]
        if row["tier"] != TIER_DEFAULT or row["threshold"] is not None:
            raise OfficialPricingError("%s: single-row model must be a Default tier with no threshold" % where)
        return {TIER_DEFAULT: row["rates"]}, None
    if len(rows) == 2:
        by_tier = {r["tier"]: r for r in rows}
        if set(by_tier) != {TIER_DEFAULT, TIER_LONG}:
            raise OfficialPricingError("%s: two-row model must have exactly Default + Long context tiers" % where)
        d_thr = by_tier[TIER_DEFAULT]["threshold"]
        l_thr = by_tier[TIER_LONG]["threshold"]
        if (d_thr is None or l_thr is None or d_thr["comparator"] != "<=" or l_thr["comparator"] != ">"
                or d_thr["k_value"] != l_thr["k_value"]):
            raise OfficialPricingError("%s: tier thresholds must be a matching '≤ N K' / '> N K' pair" % where)
        k_value = d_thr["k_value"]
        threshold = {
            "label": "%dK" % k_value,
            "tokens": k_value * 1000,
            "ambiguous_upper_tokens": k_value * 1024,
        }
        return {TIER_DEFAULT: by_tier[TIER_DEFAULT]["rates"], TIER_LONG: by_tier[TIER_LONG]["rates"]}, threshold
    raise OfficialPricingError("%s: unsupported number of tier rows (%d)" % (where, len(rows)))


def build_official_lookup(models):
    lookup = {}
    collisions = set()
    for model_id in models:
        key = official_lookup_key(model_id)
        if key in lookup and lookup[key] != model_id:
            collisions.add(key)
        lookup[key] = model_id
    for key in collisions:
        del lookup[key]
    return lookup


def build_official_snapshot(raw_bytes, fetched_epoch):
    """Validated, cacheable snapshot from the raw article-body bytes."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise OfficialPricingError("response is not valid UTF-8: %s" % ex)
    parsed = parse_official_pricing_markdown(text)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    snapshot = {
        "schema_version": OFFICIAL_PRICING_SCHEMA_VERSION,
        "kind": OFFICIAL_PRICING_KIND,
        "source_url": OFFICIAL_PRICING_API_URL,
        "public_url": OFFICIAL_PRICING_PUBLIC_URL,
        "fetched_at": _iso_from_epoch(fetched_epoch),
        "fetched_epoch": float(fetched_epoch),
        "content_sha256": digest,
        "snapshot_id": "sha256:%s" % digest[:16],
        "unit": OFFICIAL_PRICING_UNIT,
        "providers": parsed["providers"],
        "models": parsed["models"],
        "lookup": parsed["lookup"],
    }
    return validate_official_snapshot(snapshot)


def _check_rate(value, where, allow_none=False):
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise OfficialPricingError("%s: invalid rate %r" % (where, value))


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
OFFICIAL_PRICING_UNIT = "USD per 1M tokens"


def _check_str_list(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise OfficialPricingError("%s must be a list of non-empty strings" % where)


def validate_official_snapshot(snap):
    """Validate a snapshot (freshly built or read back from the cache file).
    Checks every field later used for pricing, provenance and rendering, so
    a hand-edited/corrupted cache is rejected as a whole (and re-fetched)
    instead of raising mid-ingest."""
    if not isinstance(snap, dict):
        raise OfficialPricingError("snapshot must be an object")
    if snap.get("schema_version") != OFFICIAL_PRICING_SCHEMA_VERSION or snap.get("kind") != OFFICIAL_PRICING_KIND:
        raise OfficialPricingError("unsupported snapshot schema")
    for key in ("source_url", "public_url", "fetched_at", "content_sha256", "snapshot_id"):
        if not isinstance(snap.get(key), str) or not snap[key]:
            raise OfficialPricingError("snapshot field %r missing" % key)
    for key in ("source_url", "public_url"):
        if not snap[key].startswith(OFFICIAL_PRICING_ALLOWED_URL_PREFIX):
            raise OfficialPricingError("snapshot field %r is not a %s URL" % (key, OFFICIAL_PRICING_ALLOWED_URL_PREFIX))
    if snap.get("unit") != OFFICIAL_PRICING_UNIT:
        raise OfficialPricingError("snapshot unit must be %r" % OFFICIAL_PRICING_UNIT)
    digest = snap["content_sha256"]
    if not _SHA256_HEX_RE.match(digest):
        raise OfficialPricingError("snapshot field 'content_sha256' is not a sha256 hex digest")
    if snap["snapshot_id"] != "sha256:%s" % digest[:16]:
        raise OfficialPricingError("snapshot_id does not match content_sha256")
    epoch = snap.get("fetched_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        raise OfficialPricingError("snapshot field 'fetched_epoch' invalid")
    try:
        expected_at = _iso_from_epoch(epoch) if math.isfinite(epoch) and epoch >= 0 else None
    except (OverflowError, ValueError, OSError):
        expected_at = None
    if expected_at is None or snap["fetched_at"] != expected_at:
        raise OfficialPricingError("snapshot field 'fetched_epoch' invalid or inconsistent with 'fetched_at'")
    _check_str_list(snap.get("providers"), "snapshot field 'providers'")
    models = snap.get("models")
    if not isinstance(models, dict) or not models:
        raise OfficialPricingError("snapshot has no models")
    for model_id, entry in models.items():
        if not isinstance(model_id, str) or not _MODEL_ID_RE.match(model_id):
            raise OfficialPricingError("snapshot model id %r invalid" % (model_id,))
        where = "snapshot model %r" % model_id
        if not isinstance(entry, dict) or not isinstance(entry.get("tiers"), dict):
            raise OfficialPricingError("%s: malformed entry" % where)
        for key in ("display_name", "provider"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise OfficialPricingError("%s: %r must be a non-empty string" % (where, key))
        notes = entry.get("notes")
        if notes is None:
            notes = []
        if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            raise OfficialPricingError("%s: 'notes' must be a list of strings" % where)
        for label_key in _OFFICIAL_LABEL_KEYS:
            # Optional (absent in caches written before these labels were
            # recorded); when present, a bounded non-empty string.
            label = entry.get(label_key)
            if label is not None and (not isinstance(label, str) or not label
                                      or len(label) > _OFFICIAL_LABEL_MAX_LEN):
                raise OfficialPricingError("%s: %r must be a non-empty string of at most %d characters"
                                           % (where, label_key, _OFFICIAL_LABEL_MAX_LEN))
        tiers = entry["tiers"]
        threshold = entry.get("threshold")
        if threshold is None:
            if set(tiers) != {TIER_DEFAULT}:
                raise OfficialPricingError("%s: tiers inconsistent with missing threshold" % where)
        else:
            if set(tiers) != {TIER_DEFAULT, TIER_LONG} or not isinstance(threshold, dict):
                raise OfficialPricingError("%s: tiers inconsistent with threshold" % where)
            for key in ("tokens", "ambiguous_upper_tokens"):
                v = threshold.get(key)
                if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                    raise OfficialPricingError("%s: invalid threshold %r" % (where, key))
            if threshold["ambiguous_upper_tokens"] < threshold["tokens"]:
                raise OfficialPricingError("%s: invalid threshold band" % where)
        for tier, rates in tiers.items():
            tw = "%s tier %s" % (where, tier)
            if not isinstance(rates, dict):
                raise OfficialPricingError("%s: malformed rates" % tw)
            for key in ("input", "cached_input", "output"):
                _check_rate(rates.get(key), "%s %s" % (tw, key))
            status = rates.get("cache_write_status")
            if status == CACHE_WRITE_RATE:
                _check_rate(rates.get("cache_write"), "%s cache_write" % tw)
            elif status in (CACHE_WRITE_NOT_APPLICABLE, CACHE_WRITE_NOT_LISTED):
                if rates.get("cache_write") is not None:
                    raise OfficialPricingError("%s: cache_write must be null when %s" % (tw, status))
            else:
                raise OfficialPricingError("%s: invalid cache_write_status %r" % (tw, status))
    lookup = snap.get("lookup")
    if (not isinstance(lookup, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in lookup.items())
            or lookup != build_official_lookup(models)):
        raise OfficialPricingError("snapshot lookup table invalid or inconsistent with its models")
    return snap


def read_official_snapshot_cache():
    """Returns (snapshot_or_None, error_or_None). Never raises for a
    missing/unreadable/corrupted cache: an invalid cache is rejected as a
    whole (and a refresh will re-fetch it)."""
    path = official_pricing_cache_path()
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "rb") as f:
            raw = json.loads(f.read().decode("utf-8"), parse_constant=_reject_json_constant)
        return validate_official_snapshot(raw), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as ex:
        # ValueError covers OfficialPricingError/RequestPricingError too.
        return None, "cached official pricing snapshot %s is invalid: %s" % (path, ex)


_REFRESH_STATE_EPOCH_KEYS = ("last_attempt_epoch", "last_failure_epoch", "last_success_epoch")
_REFRESH_STATE_TEXT_KEYS = ("last_attempt_at", "last_failure_at", "last_success_at", "last_error")


def validate_official_refresh_state(state):
    """Validate the refresh-state metadata. Epochs must be absent/null or
    finite, nonnegative, non-bool numbers; timestamps/last_error absent/null
    or strings; consecutive_failures absent/null or a nonnegative int.
    Unknown keys are left as-is (never read). Raises OfficialPricingError."""
    if not isinstance(state, dict):
        raise OfficialPricingError("refresh state must be a JSON object")
    for key in _REFRESH_STATE_EPOCH_KEYS:
        v = state.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise OfficialPricingError("%r must be a number, got %r" % (key, v))
        try:
            ok = math.isfinite(v) and v >= 0
        except OverflowError:
            ok = False
        if not ok:
            raise OfficialPricingError("%r must be finite and nonnegative, got %r" % (key, v))
    for key in _REFRESH_STATE_TEXT_KEYS:
        v = state.get(key)
        if v is not None and not isinstance(v, str):
            raise OfficialPricingError("%r must be a string, got %r" % (key, v))
    v = state.get("consecutive_failures")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
        raise OfficialPricingError("'consecutive_failures' must be a nonnegative integer, got %r" % (v,))
    return state


def read_official_refresh_state():
    """Returns (state_dict, error_or_None). A missing file is an empty state;
    an unreadable/corrupted one yields an EMPTY state plus an error message
    (it holds only retry metadata: resetting it never touches the cached
    snapshot or any recorded task cost). Never raises."""
    path = official_pricing_state_path()
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "rb") as f:
            raw = json.loads(f.read().decode("utf-8"), parse_constant=_reject_json_constant)
        return validate_official_refresh_state(raw), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as ex:
        return {}, ("official pricing refresh state %s is invalid (%s); its retry metadata is ignored and "
                    "reset (cached snapshot and recorded task costs are unaffected)" % (path, ex))


def refresh_official_pricing_if_due(now=None):
    """Refresh the cached official snapshot if it is missing/invalid/older
    than the TTL. At most ONE attempt per cache path per process (memoized),
    and none at all within the retry backoff after a failed attempt. Never
    raises; failures keep the previous valid cache and warn on stderr.
    Returns a small result dict (for status display/tests)."""
    path = official_pricing_cache_path()
    if path in _OFFICIAL_REFRESH_RESULTS:
        return _OFFICIAL_REFRESH_RESULTS[path]
    result = _refresh_official_pricing(time.time() if now is None else now)
    _OFFICIAL_REFRESH_RESULTS[path] = result
    return result


def refresh_official_pricing_now(now=None, min_interval_seconds=OFFICIAL_PRICING_MANUAL_MIN_INTERVAL_SECONDS):
    """Explicit, user-requested refresh (copilot-model-refresh.py / copilot-s
    menu `p`). Unlike refresh_official_pricing_if_due it ignores the 24h TTL
    and the 1h failure backoff, but it keeps everything else: the
    COPILOT_TASK_REPORT_PRICING_FETCH kill switch, the refresh lock, the same
    fixed HTTPS source, strict parsing/validation, and keep-last-valid-cache
    on failure (the failure is still recorded in the refresh state). A
    snapshot fetched less than `min_interval_seconds` ago is reused instead
    of downloaded again. Never raises; returns the same result dict shape
    (also memoized so a later automatic refresh in this process is a no-op)."""
    path = official_pricing_cache_path()
    result = _refresh_official_pricing(time.time() if now is None else now, force=True,
                                       min_interval_seconds=min_interval_seconds)
    _OFFICIAL_REFRESH_RESULTS[path] = result
    return result


def official_snapshot_age_status(age_seconds):
    """Classify a cached snapshot by its age since it was FETCHED:
    'fresh' (< TTL), 'stale' (TTL .. OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS;
    still used for pricing, labeled stale) or 'expired' (older than that, or
    dated in the future: kept for reference only, never used for pricing)."""
    if age_seconds < 0 or age_seconds > OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS:
        return "expired"
    if age_seconds < OFFICIAL_PRICING_TTL_SECONDS:
        return "fresh"
    return "stale"


def _reset_invalid_refresh_state(state_error):
    """Warn about a corrupted refresh-state file and overwrite it with an
    empty state (retry metadata only; the cache and task costs are kept)."""
    print("Warning: %s." % state_error, file=sys.stderr)
    try:
        atomic_write(official_pricing_state_path(), json.dumps({}, indent=2))
    except OSError as ex:
        print("Warning: could not reset the official pricing refresh state (%s); continuing with empty "
              "retry metadata." % ex, file=sys.stderr)


def _refresh_official_pricing(now, force=False, min_interval_seconds=OFFICIAL_PRICING_MANUAL_MIN_INTERVAL_SECONDS):
    if not official_pricing_fetch_enabled():
        return {"attempted": False, "status": "disabled"}
    try:
        with official_pricing_lock():
            state, state_error = read_official_refresh_state()
            if state_error:
                _reset_invalid_refresh_state(state_error)
            snapshot, cache_error = read_official_snapshot_cache()
            if snapshot is not None:
                age = now - snapshot["fetched_epoch"]
                reuse_window = min_interval_seconds if force else OFFICIAL_PRICING_TTL_SECONDS
                if 0 <= age < reuse_window:
                    return {"attempted": False, "status": "fresh"}
            # Validated above: epochs are None or finite nonnegative numbers.
            # An explicit (forced) refresh is a direct user request, so it
            # is not subject to the automatic failure backoff.
            last_fail = state.get("last_failure_epoch")
            last_ok = state.get("last_success_epoch") or 0
            if (not force and last_fail is not None and last_fail > last_ok
                    and 0 <= now - last_fail < OFFICIAL_PRICING_RETRY_BACKOFF_SECONDS):
                return {"attempted": False, "status": "suppressed"}

            state["last_attempt_epoch"] = now
            state["last_attempt_at"] = _iso_from_epoch(now)
            fetcher = PRICING_FETCHER or _default_pricing_fetch
            try:
                raw = fetcher(OFFICIAL_PRICING_API_URL, OFFICIAL_PRICING_TIMEOUT_SECONDS, OFFICIAL_PRICING_MAX_BYTES)
                if not isinstance(raw, (bytes, bytearray)):
                    raise OfficialPricingError("fetcher returned %s, expected bytes" % type(raw).__name__)
                if len(raw) > OFFICIAL_PRICING_MAX_BYTES:
                    raise OfficialPricingError("response too large (> %d bytes)" % OFFICIAL_PRICING_MAX_BYTES)
                new_snapshot = build_official_snapshot(bytes(raw), now)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError) as ex:
                # Network (OSError incl. URLError/timeouts/TLS), protocol and
                # parse/validation failures (OfficialPricingError is a
                # ValueError) never break ingest: keep the last valid cache.
                message = ("%s: %s" % (type(ex).__name__, ex))[:300]
                state["last_failure_epoch"] = now
                state["last_failure_at"] = _iso_from_epoch(now)
                state["last_error"] = message
                state["consecutive_failures"] = (state.get("consecutive_failures") or 0) + 1
                atomic_write(official_pricing_state_path(), json.dumps(state, indent=2))
                if snapshot is not None:
                    if official_snapshot_age_status(now - snapshot["fetched_epoch"]) == "expired":
                        kept = ("keeping the last valid snapshot fetched %s for reference only: it is older than "
                                "%d days (or dated in the future), so new calls will be recorded as unpriced"
                                % (snapshot["fetched_at"], OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS // 86400))
                    else:
                        kept = ("keeping the last valid snapshot fetched %s (now stale; used for pricing only "
                                "until it is %d days old)"
                                % (snapshot["fetched_at"], OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS // 86400))
                elif cache_error:
                    kept = "the existing cache is also invalid (%s); new calls will be recorded as unpriced" % cache_error
                else:
                    kept = "no cached snapshot exists; new calls will be recorded as unpriced (not $0)"
                print("Warning: official GitHub Copilot pricing refresh failed (%s); %s. Retries are suppressed "
                      "for %d min after a failure (retried at most hourly while failing)."
                      % (message, kept, OFFICIAL_PRICING_RETRY_BACKOFF_SECONDS // 60),
                      file=sys.stderr)
                return {"attempted": True, "status": "failed", "error": message}

            atomic_write(official_pricing_cache_path(), json.dumps(new_snapshot, indent=2, sort_keys=True))
            state["last_success_epoch"] = now
            state["last_success_at"] = _iso_from_epoch(now)
            state["last_error"] = None
            state["consecutive_failures"] = 0
            atomic_write(official_pricing_state_path(), json.dumps(state, indent=2))
            return {"attempted": True, "status": "refreshed", "snapshot_id": new_snapshot["snapshot_id"]}
    except OSError as ex:
        print("Warning: could not refresh official GitHub Copilot pricing cache (%s); using the existing "
              "cache if any." % ex, file=sys.stderr)
        return {"attempted": False, "status": "error", "error": str(ex)}


def load_official_pricing_context(now=None):
    """Cache-only (never touches the network, never raises for corrupted
    cache/state files). Returns
    {'status': 'fresh'|'stale'|'expired'|'unavailable', 'snapshot',
     'age_seconds', 'cache_error', 'state_error', 'last_error',
     'last_failure_at', 'fetch_enabled', 'path'}.
    An 'expired' snapshot is still returned (for display/reference) but is
    never used to price calls — see official_pricing_snapshot_for_ingest."""
    now = time.time() if now is None else now
    snapshot, cache_error = read_official_snapshot_cache()
    state, state_error = read_official_refresh_state()
    ctx = {
        "snapshot": snapshot,
        "cache_error": cache_error,
        "state_error": state_error,
        "last_error": state.get("last_error"),
        "last_failure_at": state.get("last_failure_at"),
        "fetch_enabled": official_pricing_fetch_enabled(),
        "path": official_pricing_cache_path(),
        "age_seconds": None,
    }
    if snapshot is None:
        ctx["status"] = "unavailable"
    else:
        age = now - snapshot["fetched_epoch"]
        ctx["age_seconds"] = age
        ctx["status"] = official_snapshot_age_status(age)
    return ctx


def official_pricing_snapshot_for_ingest(pricing_ctx):
    """(snapshot_or_None, unpriced_reason) to price calls ingested with this
    context. A snapshot whose context status is 'expired' (or whose recorded
    age exceeds the maximum pricing age / is negative) is NOT used: calls
    are recorded as unpriced with UNPRICED_SNAPSHOT_TOO_OLD."""
    ctx = pricing_ctx or {}
    snapshot = ctx.get("snapshot")
    if snapshot is None:
        return None, UNPRICED_NO_SNAPSHOT
    age = ctx.get("age_seconds")
    if ctx.get("status") == "expired" or (
            isinstance(age, (int, float)) and not isinstance(age, bool)
            and official_snapshot_age_status(age) == "expired"):
        return None, UNPRICED_SNAPSHOT_TOO_OLD
    return snapshot, None


def resolve_official_model(model, snapshot):
    """OTEL model id -> official model id, or None. Exact (case-insensitive)
    match first, then the validated dash-for-dot lookup. No other aliasing:
    anything else (e.g. 'auto', unlisted or renamed models) is unpriced."""
    if not snapshot or not isinstance(model, str):
        return None
    m = model.strip().lower()
    if m in snapshot["models"]:
        return m
    return snapshot.get("lookup", {}).get(official_lookup_key(m))


def is_gemini_official_model(model_id, entry):
    """True for an official Gemini model or any model in the Google provider
    table (conservative: Gemini telemetry may report output EXCLUDING
    reasoning tokens)."""
    provider = entry.get("provider") if isinstance(entry, dict) else None
    provider = provider.strip().lower() if isinstance(provider, str) else ""
    return model_id.startswith("gemini") or "google" in provider or "gemini" in provider


def price_call_official(mc, snapshot, no_snapshot_reason=UNPRICED_NO_SNAPSHOT):
    """Price ONE model call. Returns (result_dict, None) or (None, reason).
    Tier is chosen from THIS call's input tokens (never a task aggregate).
    `no_snapshot_reason` is the recorded reason when `snapshot` is None
    (e.g. UNPRICED_SNAPSHOT_TOO_OLD when the only cache is too old)."""
    if snapshot is None:
        return None, no_snapshot_reason
    model_id = resolve_official_model(mc.get("model"), snapshot)
    if model_id is None:
        return None, UNPRICED_UNKNOWN_MODEL
    present = mc.get("usage_present") or {}
    if not present.get("input", True):
        return None, UNPRICED_NO_INPUT
    if not present.get("output", True):
        return None, UNPRICED_NO_OUTPUT
    # Fail closed on raw usage that is present but not a strict count
    # (boolean, numeric string, fraction, negative, unsafe huge, NaN/Infinity,
    # non-numeric): the leniently converted aggregate value is not trusted as
    # known usage, so the call is never priced from it. Absent optional
    # attributes are fine. Calls built without these flags (older code
    # paths) are checked exactly as before.
    if mc.get("usage_unconvertible") or mc.get("usage_strict_ok") is False:
        return None, UNPRICED_INVALID_USAGE
    inp = int(mc.get("prompt_tokens") or 0)
    out = int(mc.get("completion_tokens") or 0)
    reasoning = int(mc.get("reasoning_tokens") or 0)
    cache_read = int(mc.get("cache_read_tokens") or 0)
    cache_write = int(mc.get("cache_write_tokens") or 0)
    if min(inp, out, reasoning, cache_read, cache_write) < 0:
        return None, UNPRICED_NEGATIVE
    if cache_read + cache_write > inp:
        return None, UNPRICED_CACHE_EXCEEDS_INPUT

    entry = snapshot["models"][model_id]
    if reasoning > 0 and is_gemini_official_model(model_id, entry):
        # Undetectable when reasoning <= output, so never priced (no guess).
        return None, UNPRICED_GEMINI_REASONING
    if reasoning > out:
        return None, UNPRICED_REASONING_EXCEEDS_OUTPUT

    threshold = entry.get("threshold")
    if threshold is None:
        tier = TIER_DEFAULT
    elif inp <= threshold["tokens"]:
        tier = TIER_DEFAULT
    elif inp <= threshold["ambiguous_upper_tokens"]:
        return None, UNPRICED_TIER_AMBIGUOUS
    else:
        tier = TIER_LONG
    rates = entry["tiers"][tier]

    status = rates["cache_write_status"]
    if status == CACHE_WRITE_RATE:
        cache_write_rate = rates["cache_write"]
    elif status == CACHE_WRITE_NOT_APPLICABLE:
        # Official "Not applicable": no separate cache-write cost — those
        # tokens are still ordinary input tokens, billed at the input rate.
        cache_write_rate = rates["input"]
    elif cache_write > 0:
        return None, UNPRICED_CACHE_WRITE_NOT_LISTED
    else:
        cache_write_rate = 0.0

    fresh_input = inp - cache_read - cache_write
    components = {
        "input_usd": fresh_input * rates["input"] / 1_000_000.0,
        "cached_input_usd": cache_read * rates["cached_input"] / 1_000_000.0,
        "cache_write_usd": cache_write * cache_write_rate / 1_000_000.0,
        # Reasoning tokens are a subset of output: priced once, here.
        "output_usd": out * rates["output"] / 1_000_000.0,
    }
    return {"model_id": model_id, "tier": tier, "usd": sum(components.values()), "components": components}, None


def _blank_official_bucket():
    return {"calls": 0, "usd": 0.0, "input_usd": 0.0, "cached_input_usd": 0.0, "cache_write_usd": 0.0,
            "output_usd": 0.0, "official_model": None, "tiers": {}}


def build_official_cost_delta(calls, pricing_ctx):
    """Additive per-call official cost buckets for one ingest batch."""
    snapshot, no_snapshot_reason = official_pricing_snapshot_for_ingest(pricing_ctx)
    out = {"snapshot": None, "by_model": {}}
    used_ids = set()
    for mc in calls:
        model = mc["model"]
        rec = out["by_model"].setdefault(model, {"priced": {}, "unpriced": {}})
        result, reason = price_call_official(mc, snapshot, no_snapshot_reason or UNPRICED_NO_SNAPSHOT)
        if result is None:
            # Frozen per-call outcome for the feature-usage call ledger
            # (recorded once, never repriced).
            mc["official_pricing"] = {"status": "unpriced", "reason": reason}
            u = rec["unpriced"].setdefault(reason, {"calls": 0, "total_tokens": 0})
            u["calls"] += 1
            u["total_tokens"] += int(mc.get("total_tokens") or 0)
            continue
        # Per-call estimate kept on the call record (not persisted on its
        # own) so attribution can sum exactly what was recorded here.
        mc["official_usd"] = result["usd"]
        mc["official_pricing"] = {
            "status": "priced",
            "snapshot_id": snapshot["snapshot_id"],
            "official_model": result["model_id"],
            "tier": result["tier"],
            "usd": result["usd"],
            "components": dict(result["components"]),
        }
        bucket = rec["priced"].setdefault(snapshot["snapshot_id"], _blank_official_bucket())
        bucket["calls"] += 1
        bucket["usd"] += result["usd"]
        for key, value in result["components"].items():
            bucket[key] += value
        bucket["official_model"] = result["model_id"]
        bucket["tiers"][result["tier"]] = bucket["tiers"].get(result["tier"], 0) + 1
        used_ids.add(result["model_id"])
    if snapshot is not None and used_ids:
        age = (pricing_ctx or {}).get("age_seconds")
        out["snapshot"] = {
            "snapshot_id": snapshot["snapshot_id"],
            "fetched_at": snapshot["fetched_at"],
            "content_sha256": snapshot["content_sha256"],
            "source_url": snapshot["source_url"],
            "public_url": snapshot["public_url"],
            "stale_at_ingest": (pricing_ctx or {}).get("status") == "stale",
            "age_hours_at_ingest": round(age / 3600.0, 2) if isinstance(age, (int, float)) else None,
            "notes": {mid: list(snapshot["models"][mid].get("notes") or []) for mid in sorted(used_ids)
                      if snapshot["models"][mid].get("notes")},
        }
    return out


def merge_official_cost_delta(task, oc_delta):
    if not oc_delta or not oc_delta.get("by_model"):
        return
    oc = task.get("official_cost")
    if not isinstance(oc, dict):
        oc = task["official_cost"] = {
            "schema_version": OFFICIAL_COST_SCHEMA_VERSION,
            "since": now_iso(),
            "snapshots": {},
            "by_model": {},
        }
    snaps = oc.setdefault("snapshots", {})
    meta = oc_delta.get("snapshot")
    if meta:
        sid = meta["snapshot_id"]
        dst = snaps.setdefault(sid, {
            "content_sha256": meta["content_sha256"],
            "source_url": meta["source_url"],
            "public_url": meta["public_url"],
            "first_fetched_at": meta["fetched_at"],
            "first_used_at": now_iso(),
            "used_while_stale": False,
            "max_age_hours_at_ingest": None,
            "notes": {},
        })
        dst["last_fetched_at"] = meta["fetched_at"]
        dst["last_used_at"] = now_iso()
        dst["used_while_stale"] = bool(dst.get("used_while_stale")) or bool(meta.get("stale_at_ingest"))
        age = meta.get("age_hours_at_ingest")
        if age is not None and (dst.get("max_age_hours_at_ingest") is None or age > dst["max_age_hours_at_ingest"]):
            dst["max_age_hours_at_ingest"] = age
        notes = dst.setdefault("notes", {})
        for mid, texts in (meta.get("notes") or {}).items():
            existing = notes.setdefault(mid, [])
            for t in texts:
                if t not in existing:
                    existing.append(t)
    by_model = oc.setdefault("by_model", {})
    for model, rec in oc_delta["by_model"].items():
        dst_rec = by_model.setdefault(model, {"priced": {}, "unpriced": {}})
        for sid, bucket in rec.get("priced", {}).items():
            b = dst_rec.setdefault("priced", {}).setdefault(sid, _blank_official_bucket())
            for key in ("calls", "usd", "input_usd", "cached_input_usd", "cache_write_usd", "output_usd"):
                b[key] = b.get(key, 0) + bucket[key]
            b["official_model"] = bucket.get("official_model") or b.get("official_model")
            tiers = b.setdefault("tiers", {})
            for tier, n in bucket.get("tiers", {}).items():
                tiers[tier] = tiers.get(tier, 0) + n
        for reason, u in rec.get("unpriced", {}).items():
            d = dst_rec.setdefault("unpriced", {}).setdefault(reason, {"calls": 0, "total_tokens": 0})
            d["calls"] += u["calls"]
            d["total_tokens"] += u["total_tokens"]


def summarize_official_cost(task):
    """Coverage summary from RECORDED estimates (never recomputed)."""
    oc = task.get("official_cost") if isinstance(task.get("official_cost"), dict) else {}
    recorded = oc.get("by_model") or {}
    token_models = task.get("by_model") or {}
    per_model = {}
    totals = {"calls": 0, "priced_calls": 0, "usd": 0.0, "unpriced_calls": 0, "legacy_calls": 0}
    reasons = {}
    warnings = []
    for model in sorted(set(token_models) | set(recorded)):
        rec = recorded.get(model) or {}
        priced_calls = sum(int(b.get("calls", 0) or 0) for b in (rec.get("priced") or {}).values())
        usd = sum(float(b.get("usd", 0) or 0) for b in (rec.get("priced") or {}).values())
        official_models = sorted({b.get("official_model") for b in (rec.get("priced") or {}).values()
                                  if b.get("official_model")})
        tiers = {}
        for b in (rec.get("priced") or {}).values():
            for tier, n in (b.get("tiers") or {}).items():
                tiers[tier] = tiers.get(tier, 0) + n
        unpriced = {r: int(u.get("calls", 0) or 0) for r, u in (rec.get("unpriced") or {}).items()}
        unpriced_calls = sum(unpriced.values())
        calls = int((token_models.get(model) or {}).get("call_count", 0) or 0)
        legacy = calls - priced_calls - unpriced_calls
        if legacy < 0:
            warnings.append("model '%s': recorded cost buckets (%d calls) exceed the token totals (%d calls)"
                            % (model, priced_calls + unpriced_calls, calls))
            legacy = 0
        per_model[model] = {
            "calls": calls, "priced_calls": priced_calls, "usd": usd, "unpriced": unpriced,
            "unpriced_calls": unpriced_calls, "legacy_calls": legacy,
            "official_models": official_models, "tiers": tiers,
        }
        totals["calls"] += calls
        totals["priced_calls"] += priced_calls
        totals["usd"] += usd
        totals["unpriced_calls"] += unpriced_calls
        totals["legacy_calls"] += legacy
        for r, n in unpriced.items():
            reasons[r] = reasons.get(r, 0) + n
    return {"per_model": per_model, "totals": totals, "reasons": reasons, "warnings": warnings,
            "snapshots": oc.get("snapshots") or {}, "since": oc.get("since")}


def format_official_cost_total(summary):
    t = summary["totals"]
    if t["calls"] == 0 and t["priced_calls"] == 0:
        return "$0.0000 (no model calls recorded)"
    if t["priced_calls"] == 0:
        return ("N/A — none of the %d recorded calls could be priced (see Estimated Cost Coverage); "
                "this is *not* $0" % t["calls"])
    if t["unpriced_calls"] == 0 and t["legacy_calls"] == 0:
        return "$%.4f — all %d recorded calls priced" % (t["usd"], t["priced_calls"])
    parts = []
    if t["unpriced_calls"]:
        parts.append("%d unpriced" % t["unpriced_calls"])
    if t["legacy_calls"]:
        parts.append("%d legacy (recorded before official per-token pricing, never repriced)" % t["legacy_calls"])
    return "$%.4f — **PARTIAL (lower bound)**: %d of %d calls priced; excludes %s" % (
        t["usd"], t["priced_calls"], t["calls"], ", ".join(parts))


def format_official_model_cost(row):
    if row["priced_calls"] == 0:
        if row["calls"] == 0 and not row["unpriced_calls"]:
            return "—"
        return "not priced"
    s = "$%.4f" % row["usd"]
    missing = row["unpriced_calls"] + row["legacy_calls"]
    if missing:
        s += " (partial: %d of %d calls)" % (row["priced_calls"], row["calls"])
    return s


def _fmt_age(seconds):
    if seconds is None:
        return "unknown age"
    if seconds < 0:
        return "fetched in the future (clock skew)"
    hours = seconds / 3600.0
    if hours < 48:
        return "%.1fh old" % hours
    return "%.1f days old" % (hours / 24.0)


def render_official_cost_section(task, summary, ctx):
    lines = []
    lines.append("## Estimated Cost Coverage (official GitHub Copilot per-token rates)")
    lines.append("")
    lines.append("_Each model call is priced individually **at ingestion** with GitHub's official published "
                 "per-token rates ([Models and pricing for GitHub Copilot](%s)), fetched automatically and "
                 "cached for 24h, and the result is recorded with the snapshot that priced it — a later rate "
                 "change never reprices past calls. If ingestion was delayed, the rates are those published "
                 "at ingestion time, not necessarily the rates in force when the call happened. The tier "
                 "(Default / Long context) is chosen from each call's own input tokens. Cached-input and "
                 "cache-write tokens are subsets of input tokens (priced at their own rates; a cache write "
                 "listed as \"Not applicable\" is billed as ordinary input); reasoning tokens are part of "
                 "output and priced once. This is an **estimate of list-price usage, not a bill**: it does "
                 "not deduct plan-included GitHub AI Credits allowances, and calls that cannot be priced "
                 "exactly are excluded and listed below (never counted as $0)._" % OFFICIAL_PRICING_PUBLIC_URL)
    lines.append("")

    # Current cache status (affects FUTURE ingests only; recorded costs are fixed).
    if ctx["status"] == "fresh":
        status = "fresh — snapshot `%s` fetched %s (%s; refreshed automatically every 24h)" % (
            ctx["snapshot"]["snapshot_id"], ctx["snapshot"]["fetched_at"], _fmt_age(ctx["age_seconds"]))
    elif ctx["status"] == "stale":
        status = ("**STALE** — last valid snapshot `%s` fetched %s (%s, older than 24h; still used for pricing "
                  "until it is %d days old)" % (
                      ctx["snapshot"]["snapshot_id"], ctx["snapshot"]["fetched_at"], _fmt_age(ctx["age_seconds"]),
                      OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS // 86400))
    elif ctx["status"] == "expired":
        status = ("**TOO OLD TO PRICE** — last valid snapshot `%s` fetched %s (%s; more than %d days old or "
                  "dated in the future) is kept for reference only; calls ingested now are recorded as unpriced "
                  "(not $0). Its fetch time is when this tool downloaded the page, not a rate effective date" % (
                      ctx["snapshot"]["snapshot_id"], ctx["snapshot"]["fetched_at"], _fmt_age(ctx["age_seconds"]),
                      OFFICIAL_PRICING_MAX_PRICING_AGE_SECONDS // 86400))
    else:
        status = ("**UNAVAILABLE** — no valid official pricing snapshot is cached; calls ingested now are "
                  "recorded as unpriced (not $0)")
    extras = []
    if ctx.get("cache_error"):
        extras.append(ctx["cache_error"])
    if ctx.get("state_error"):
        extras.append(ctx["state_error"])
    if ctx["status"] != "fresh" and ctx.get("last_error"):
        extras.append("last refresh error at %s: %s" % (ctx.get("last_failure_at") or "unknown time", ctx["last_error"]))
    if not ctx.get("fetch_enabled"):
        extras.append("automatic fetching is disabled via %s" % OFFICIAL_PRICING_FETCH_ENV)
    lines.append("- Official rate cache now: %s%s" % (status, (" — " + "; ".join(extras)) if extras else ""))
    lines.append("  _(cache status only affects calls ingested from now on; the estimates below are as recorded.)_")
    if summary.get("since"):
        lines.append("- Official per-token estimates recorded for this task since: %s" % summary["since"])
    lines.append("")

    t = summary["totals"]
    lines.append("| Model | Calls | Priced calls | Est. USD (official rates) | Unpriced calls | Legacy calls | Official model / tiers used |")
    lines.append("|---|---|---|---|---|---|---|")
    for model, row in sorted(summary["per_model"].items(), key=lambda kv: (-kv[1]["usd"], kv[0])):
        tiers = ", ".join("%s×%d" % (TIER_LABELS.get(k, k), n) for k, n in sorted(row["tiers"].items()))
        official = ", ".join(row["official_models"]) or "—"
        lines.append("| %s | %d | %d | %s | %d | %d | %s%s |" % (
            model, row["calls"], row["priced_calls"],
            ("$%.4f" % row["usd"]) if row["priced_calls"] else "not priced",
            row["unpriced_calls"], row["legacy_calls"], official, (" (%s)" % tiers) if tiers else ""))
    lines.append("| **Total** | %d | %d | %s | %d | %d | |" % (
        t["calls"], t["priced_calls"], ("$%.4f" % t["usd"]) if t["priced_calls"] else "N/A",
        t["unpriced_calls"], t["legacy_calls"]))
    lines.append("")

    warnings = []
    for reason, n in sorted(summary["reasons"].items()):
        warnings.append("%d call(s) not priced: %s" % (n, reason))
    if t["legacy_calls"]:
        warnings.append("%d call(s) are legacy: recorded before official per-token pricing existed for this "
                        "task (token counts kept; no price provenance, so never backfilled or repriced)"
                        % t["legacy_calls"])
    warnings.extend(summary["warnings"])
    if warnings:
        lines.append("### Unpriced / partial-coverage warnings")
        lines.append("")
        for w in warnings:
            lines.append("- %s" % w)
        lines.append("")

    if summary["snapshots"]:
        lines.append("### Official rate snapshots used by this task")
        lines.append("")
        for sid, meta in sorted(summary["snapshots"].items(), key=lambda kv: kv[1].get("first_used_at") or ""):
            stale = " — **used while stale** (max age at ingestion %.1fh)" % meta["max_age_hours_at_ingest"] \
                if meta.get("used_while_stale") and meta.get("max_age_hours_at_ingest") is not None else ""
            fetched = meta.get("first_fetched_at") or "unknown"
            if meta.get("last_fetched_at") and meta.get("last_fetched_at") != fetched:
                fetched = "%s … %s" % (fetched, meta["last_fetched_at"])
            lines.append("- `%s` (sha256 %s…) fetched %s from %s; used %s → %s%s" % (
                sid, (meta.get("content_sha256") or "")[:12], fetched, meta.get("public_url") or meta.get("source_url"),
                meta.get("first_used_at") or "?", meta.get("last_used_at") or "?", stale))
            for mid, texts in sorted((meta.get("notes") or {}).items()):
                for text in texts:
                    lines.append("  - Official note for %s: %s" % (mid, text))
        lines.append("")
    return lines


# --------------------------------------------------------------------------
# Legacy independent pricing table (model-pricing.json) — INACTIVE
# --------------------------------------------------------------------------
#
# Kept only for backward compatibility of callers/tests. The default report
# no longer uses these approximate, non-official rates; the estimated USD
# cost comes from the official GitHub snapshot above. An existing
# ~/.copilot/task-reports/model-pricing.json is left untouched.

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
# Company fixed per-request charge policy — DORMANT (not in default report)
# --------------------------------------------------------------------------
#
# No longer rendered by render_markdown(): the fixed per-request figures
# were benchmark-derived and misleading next to official per-token rates.
# Kept for compatibility (and so the preserved request-pricing.json and the
# recorded by_model_effort data remain usable if explicitly called).
#
# A SEPARATE billing view from the token-based USD estimate above. The
# company charges a fixed USD amount per MODEL REQUEST, keyed by
# (model, reasoning-effort level), using company-configured fixed
# per-request rates (each model entry lists its own source). This is a
# company policy, NOT official GitHub/Copilot pricing, and the computed
# amount is never a verified bill.
#
# Rules (deliberately strict — a wrong charge is worse than no charge):
#   - Missing config file -> "unavailable", never $0.
#   - Invalid config (bad JSON, non-finite/negative/boolean/non-numeric
#     rates, wrong shape) -> rejected as a whole with an explicit error;
#     nothing is computed from a partially valid file.
#   - A request is priced only when its model resolves to a configured
#     entry AND its effort level is known AND a non-null rate exists for
#     that exact level. Unknown effort is never priced (no fallback level).
#   - Effort source (measured/configured/inferred) is kept distinct per
#     row and per subtotal; configured/inferred-effort charges are labeled
#     as estimates because the effort level itself was not measured.
#   - Requests recorded before joint model+effort tracking existed are
#     reported as explicitly unattributed — never backfilled/guessed.

FIXED_CHARGE_EFFORT_SOURCES = ("measured", "configured", "inferred")
FIXED_CHARGE_SOURCE_LABELS = {
    "measured": "measured effort (per-call OTEL `gen_ai.request.reasoning.level`)",
    "configured": "configured effort — ESTIMATE (session-level effort setting at call time, not per-call telemetry)",
    "inferred": "inferred effort — ESTIMATE (guessed from custom-agent role mapping, not telemetry)",
}
REQUEST_PRICING_UNIT = "model_request"


class RequestPricingError(ValueError):
    """Raised for an invalid company request-pricing config."""


def request_pricing_path():
    """Resolve the company request-pricing config path at call time, so a
    repointed SUPPORT_DIR (or an explicit REQUEST_PRICING_FILE override)
    is always respected."""
    if REQUEST_PRICING_FILE:
        return REQUEST_PRICING_FILE
    return os.path.join(SUPPORT_DIR, REQUEST_PRICING_FILENAME)


def _reject_json_constant(name):
    raise RequestPricingError("non-finite number %r is not allowed" % name)


def _validate_request_rate(value, where):
    """A rate is either null (explicitly unpriced) or a finite, nonnegative
    int/float. Booleans are rejected even though bool is an int subclass."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestPricingError(
            "%s must be a finite nonnegative number or null (got %s %r)"
            % (where, type(value).__name__, value)
        )
    if not math.isfinite(value):
        raise RequestPricingError("%s must be finite (got %r)" % (where, value))
    if value < 0:
        raise RequestPricingError("%s must be nonnegative (got %r)" % (where, value))
    return float(value)


def _validate_optional_str(value, where):
    if value is not None and not isinstance(value, str):
        raise RequestPricingError("%s must be a string if present (got %s)" % (where, type(value).__name__))
    return value


def validate_request_pricing(raw):
    """Validate a parsed request-pricing document and return a normalized
    dict. Raises RequestPricingError on ANY problem — the whole file is
    rejected rather than partially used."""
    if not isinstance(raw, dict):
        raise RequestPricingError("top level must be a JSON object")
    unit = raw.get("unit", REQUEST_PRICING_UNIT)
    if unit != REQUEST_PRICING_UNIT:
        raise RequestPricingError("'unit' must be %r (got %r)" % (REQUEST_PRICING_UNIT, unit))
    currency = raw.get("currency", "USD")
    if currency != "USD":
        raise RequestPricingError("'currency' must be 'USD' (got %r)" % (currency,))
    policy_label = _validate_optional_str(raw.get("policy_label"), "'policy_label'")
    updated_at = _validate_optional_str(raw.get("updated_at"), "'updated_at'")

    models = raw.get("models")
    if not isinstance(models, dict):
        raise RequestPricingError("'models' must be an object mapping model id -> {\"rates\": {...}}")
    norm_models = {}
    for model, entry in models.items():
        where = "models[%r]" % model
        if not isinstance(entry, dict):
            raise RequestPricingError("%s must be an object" % where)
        rates = entry.get("rates")
        if not isinstance(rates, dict):
            raise RequestPricingError("%s.rates must be an object mapping effort level -> USD per request" % where)
        norm_rates = {}
        for level, value in rates.items():
            norm_level = level.strip().lower() if isinstance(level, str) else ""
            if not norm_level or norm_level.startswith("unknown"):
                raise RequestPricingError("%s.rates has an invalid effort level key %r" % (where, level))
            if norm_level in norm_rates:
                raise RequestPricingError("%s.rates has a duplicate effort level %r" % (where, norm_level))
            norm_rates[norm_level] = _validate_request_rate(value, "%s.rates[%r]" % (where, level))
        norm_models[model] = {
            "rates": norm_rates,
            "source": _validate_optional_str(entry.get("source"), "%s.source" % where),
            "note": _validate_optional_str(entry.get("note"), "%s.note" % where),
        }

    aliases = raw.get("aliases", {})
    if aliases is None:
        aliases = {}
    if not isinstance(aliases, dict):
        raise RequestPricingError("'aliases' must be an object mapping alias -> model id (or null)")
    for alias, target in aliases.items():
        if target is not None and not isinstance(target, str):
            raise RequestPricingError("aliases[%r] must be a model id string or null" % alias)

    return {
        "policy_label": policy_label,
        "updated_at": updated_at or "unknown",
        "currency": currency,
        "models": norm_models,
        "aliases": dict(aliases),
    }


def load_request_pricing():
    """Load the company request-pricing config. Always returns a dict with
    'status' in {'ok', 'missing', 'invalid'} and 'path'; 'invalid' also
    carries 'error'. A missing/invalid config is NEVER treated as $0."""
    path = request_pricing_path()
    if not os.path.exists(path):
        return {"status": "missing", "path": path}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f, parse_constant=_reject_json_constant)
        cfg = validate_request_pricing(raw)
    except RequestPricingError as ex:
        return {"status": "invalid", "path": path, "error": str(ex)}
    except json.JSONDecodeError as ex:
        return {"status": "invalid", "path": path, "error": "invalid JSON: %s" % ex}
    except (OSError, UnicodeDecodeError) as ex:
        # Must precede the ValueError handler: UnicodeDecodeError is a
        # ValueError subclass and would otherwise be misreported as a
        # numeric error.
        return {"status": "invalid", "path": path, "error": "unreadable: %s" % ex}
    except (ValueError, OverflowError) as ex:
        # e.g. an integer literal too large for float conversion in
        # math.isfinite (OverflowError), or an overlong numeric literal
        # exceeding the int digit limit (ValueError). Reported as invalid,
        # never allowed to crash the report.
        return {"status": "invalid", "path": path, "error": "invalid numeric value: %s" % ex}
    cfg["status"] = "ok"
    cfg["path"] = path
    return cfg


def resolve_request_rate_entry(model, models, aliases):
    """Returns (entry_or_None, canonical_model_or_None, reason_if_None).
    Alias chains are followed with a cycle guard; an alias to null means
    "explicitly unpriced"."""
    seen = set()
    cur = model
    while True:
        if cur in seen:
            return None, None, "alias cycle while resolving model '%s'" % model
        seen.add(cur)
        if cur in models:
            return models[cur], cur, None
        if cur in aliases:
            target = aliases[cur]
            if target is None:
                return None, None, "model '%s' is explicitly unpriced (alias -> null)" % model
            cur = target
            continue
        return None, None, "no request rate configured for model '%s'" % model


def parse_effort_key(effort_key):
    """Split a stored effort key ('source:level' or 'unknown') into
    (source, level_or_None). Any 'unknown...' level yields None."""
    if not isinstance(effort_key, str) or ":" not in effort_key:
        return "unknown", None
    source, level = effort_key.split(":", 1)
    source = source.strip().lower()
    level = level.strip().lower()
    if not level or level.startswith("unknown"):
        level = None
    return source, level


def joint_unattributed_calls(task):
    """Per-model count of requests that have NO joint model+effort record
    (recorded before joint tracking existed). Returns (dict model->count,
    list_of_inconsistency_warnings). Never guesses a split."""
    by_model = task.get("by_model") or {}
    joint = task.get("by_model_effort") or {}
    unattributed = {}
    warnings = []
    for model in sorted(set(by_model) | set(joint)):
        model_calls = int((by_model.get(model) or {}).get("call_count", 0) or 0)
        joint_calls = sum(int((agg or {}).get("call_count", 0) or 0) for agg in (joint.get(model) or {}).values())
        diff = model_calls - joint_calls
        if diff > 0:
            unattributed[model] = diff
        elif diff < 0:
            warnings.append(
                "model '%s': joint model+effort records (%d requests) exceed by-model total (%d); "
                "joint rows are shown as recorded, totals may be inconsistent" % (model, joint_calls, model_calls)
            )
    return unattributed, warnings


def compute_fixed_charges(task, cfg):
    """Compute company fixed per-request charges from the joint
    model+effort aggregation. `cfg` must be a load_request_pricing() result
    with status 'ok'."""
    models = cfg["models"]
    aliases = cfg["aliases"]
    rows = []
    by_source = {src: {"charge": 0.0, "requests": 0} for src in FIXED_CHARGE_EFFORT_SOURCES}
    priced_requests = 0
    unpriced_requests = 0
    missing = {}  # reason -> request count
    used_models = {}  # canonical model -> entry (for rate-source notes)

    joint = task.get("by_model_effort") or {}
    for model in sorted(joint):
        entry, canonical, model_reason = resolve_request_rate_entry(model, models, aliases)
        for effort_key in sorted(joint[model]):
            agg = joint[model][effort_key] or {}
            n = int(agg.get("call_count", 0) or 0)
            if n <= 0:
                continue
            source, level = parse_effort_key(effort_key)
            row = {
                "model": model,
                "canonical_model": canonical,
                "effort_key": effort_key,
                "source": source,
                "level": level,
                "requests": n,
                "rate": None,
                "charge": None,
                "reason": None,
            }
            if source not in FIXED_CHARGE_EFFORT_SOURCES or level is None:
                row["reason"] = "effort unknown — not priced (no fallback effort level is assumed)"
            elif entry is None:
                row["reason"] = model_reason
            elif level not in entry["rates"]:
                row["reason"] = "no configured rate for model '%s' at effort '%s'" % (canonical, level)
            elif entry["rates"][level] is None:
                detail = entry.get("note") or "rate configured as null"
                row["reason"] = "explicitly unpriced: '%s' at effort '%s' (%s)" % (canonical, level, detail)
            else:
                rate = entry["rates"][level]
                row["rate"] = rate
                row["charge"] = rate * n
                used_models[canonical] = entry
            if row["charge"] is None:
                unpriced_requests += n
                missing[row["reason"]] = missing.get(row["reason"], 0) + n
            else:
                priced_requests += n
                by_source[source]["charge"] += row["charge"]
                by_source[source]["requests"] += n
            rows.append(row)

    unattributed, consistency_warnings = joint_unattributed_calls(task)
    unattributed_requests = sum(unattributed.values())
    total_requests = int((task.get("totals") or {}).get("call_count", 0) or 0)
    total_charge = sum(v["charge"] for v in by_source.values())
    return {
        "rows": rows,
        "by_source": by_source,
        "priced_requests": priced_requests,
        "unpriced_requests": unpriced_requests,
        "unattributed": unattributed,
        "unattributed_requests": unattributed_requests,
        "total_requests": total_requests,
        "total_charge": total_charge,
        "missing": missing,
        "used_models": used_models,
        "consistency_warnings": consistency_warnings,
        "complete": unpriced_requests == 0 and unattributed_requests == 0,
    }


def render_fixed_charge_section(task):
    """Markdown lines for the company fixed per-request charge section."""
    lines = []
    lines.append("## Company Fixed Per-Request Charge (company-configured policy — NOT official GitHub pricing)")
    lines.append("")
    lines.append("_A company-configured policy: a fixed USD amount per **model request**, keyed by "
                  "model + reasoning-effort level, using company-configured fixed per-request rates "
                  "(the source of each rate is listed per model below). This is **not** official GitHub/Copilot pricing and "
                  "is **never a verified bill**. Requests counted are the OTEL usage-bearing model "
                  "(`chat *`) spans recorded for this task — including retried/failed calls that "
                  "reported usage; spans without usage data are not counted, so this cannot claim that "
                  "every request was captured. Kept entirely separate from (and not comparable/additive "
                  "with) the token-based USD estimate._")
    lines.append("")

    cfg = load_request_pricing()
    if cfg["status"] == "missing":
        lines.append("**UNAVAILABLE** — no company request-pricing config found at `%s`. "
                      "This is *not* a $0 charge. Install it with `scripts/install.sh` (copied only "
                      "if absent) or copy `config/request-pricing.json` there." % cfg["path"])
        lines.append("")
        return lines
    if cfg["status"] == "invalid":
        print("Warning: invalid company request-pricing config %s: %s" % (cfg["path"], cfg["error"]),
              file=sys.stderr)
        lines.append("**UNAVAILABLE** — invalid company request-pricing config at `%s`: %s. "
                      "The whole file is rejected (no charge is computed from a partially valid "
                      "config); this is *not* a $0 charge." % (cfg["path"], cfg["error"]))
        lines.append("")
        return lines

    result = compute_fixed_charges(task, cfg)
    n_total = result["total_requests"]
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append("| Policy | %s (config updated %s) |" % (
        cfg.get("policy_label") or "Company-configured fixed per-request rates (per-model sources listed below) — not official GitHub pricing",
        cfg["updated_at"]))
    lines.append("| Model requests recorded (OTEL usage-bearing model spans, incl. retried/failed calls that reported usage) | %d |" % n_total)
    lines.append("| ...priced with a configured rate | %d |" % result["priced_requests"])
    lines.append("| ...not priced (unknown effort / no configured rate / explicitly unpriced) | %d |" % result["unpriced_requests"])
    lines.append("| ...unattributed (recorded before joint model+effort tracking — no backfill, not priced) | %d |" % result["unattributed_requests"])
    for src in FIXED_CHARGE_EFFORT_SOURCES:
        sub = result["by_source"][src]
        lines.append("| Charge — %s | $%.4f (%d requests) |" % (FIXED_CHARGE_SOURCE_LABELS[src], sub["charge"], sub["requests"]))
    if n_total == 0 and not result["rows"]:
        total_str = "$0.0000 (no model requests recorded)"
    elif result["priced_requests"] == 0:
        total_str = "N/A — none of the recorded requests could be priced (see warnings below); this is *not* $0"
    elif result["complete"]:
        total_str = "$%.4f — all %d recorded requests priced (company policy amount, not a verified bill)" % (
            result["total_charge"], result["priced_requests"])
    else:
        total_str = ("$%.4f — **PARTIAL (lower bound)**: excludes %d not-priced and %d unattributed "
                     "requests (company policy amount, not a verified bill)" % (
                         result["total_charge"], result["unpriced_requests"], result["unattributed_requests"]))
    lines.append("| Total company fixed charge | %s |" % total_str)
    lines.append("")

    lines.append("### By Model + Effort (fixed per-request charge)")
    lines.append("")
    if task.get("by_model_effort_since"):
        lines.append("_Joint model+effort tracking for this task started at %s; requests recorded "
                      "earlier have no joint attribution and are listed as unattributed._"
                      % task["by_model_effort_since"])
        lines.append("")
    lines.append("| Model | Effort source | Effort level | Requests | Rate (USD/request) | Charge | Status |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in result["rows"]:
        if row["charge"] is not None:
            status = "priced"
            if row["source"] != "measured":
                status = "priced — ESTIMATE (%s effort, not measured)" % row["source"]
            rate_str = "$%.4f" % row["rate"]
            charge_str = "$%.4f" % row["charge"]
        else:
            status = "not priced: %s" % row["reason"]
            rate_str = "—"
            charge_str = "not priced"
        lines.append("| %s | %s | %s | %d | %s | %s | %s |" % (
            row["model"], row["source"], row["level"] or "unknown", row["requests"], rate_str, charge_str, status))
    for model, n in sorted(result["unattributed"].items()):
        lines.append("| %s | — | — | %d | — | not priced | unattributed: recorded before joint "
                      "model+effort tracking (no backfill/guess) |" % (model, n))
    lines.append("")

    warnings = []
    for reason, n in sorted(result["missing"].items()):
        warnings.append("%s — %d request(s) not priced" % (reason, n))
    if result["unattributed_requests"]:
        warnings.append("%d request(s) recorded before joint model+effort tracking cannot be priced "
                        "(no backfill)" % result["unattributed_requests"])
    warnings.extend(result["consistency_warnings"])
    if warnings:
        lines.append("### Missing-rate / partial-total warnings")
        lines.append("")
        for w in warnings:
            lines.append("- %s" % w)
        lines.append("")

    if result["used_models"]:
        lines.append("### Configured rate sources")
        lines.append("")
        for model, entry in sorted(result["used_models"].items()):
            rates = ", ".join(
                "%s=%s" % (lvl, ("$%.4f" % r) if r is not None else "unpriced")
                for lvl, r in sorted(entry["rates"].items())
            )
            extra = []
            if entry.get("source"):
                extra.append("source: %s" % entry["source"])
            if entry.get("note"):
                extra.append(entry["note"])
            lines.append("- %s: %s%s" % (model, rates, (" — " + "; ".join(extra)) if extra else ""))
        lines.append("")
    return lines


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


def render_markdown(task):
    lines = []
    key = task.get("task_id", UNASSIGNED)
    lines.append("# Copilot Task Usage Report — %s" % key)
    lines.append("")
    lines.append("_Cumulative, incremental report. Tracks only usage recorded since this "
                  "feature was installed on this machine — no historical backfill._")
    lines.append("")
    lines.append("- Report last updated: %s" % task.get("updated_at", "unknown"))
    lines.append("- First recorded: %s" % task.get("created_at", "unknown"))
    lines.append("- Copilot sessions contributing: %d" % len(task.get("sessions", [])))
    repos = task.get("repositories", [])
    if repos:
        lines.append("- Repositories: %s" % ", ".join(repos))
    lines.append("")

    totals = task.get("totals", blank_agg())
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append("| Model calls (from OTEL chat spans; includes failed calls that reported usage) | %d |" % totals.get("call_count", 0))
    lines.append("| Prompt (input) tokens | %d |" % totals.get("prompt_tokens", 0))
    lines.append("| ...of which cache-read tokens | %d |" % totals.get("cache_read_tokens", 0))
    lines.append("| ...of which cache-write tokens | %d |" % totals.get("cache_write_tokens", 0))
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

    official_summary = summarize_official_cost(task)
    pricing_ctx = load_official_pricing_context()
    lines.append("| Estimated USD cost (official GitHub per-token rates recorded at ingestion — estimate, NOT a bill) | %s |"
                 % format_official_cost_total(official_summary))
    if pricing_ctx["status"] == "fresh":
        rate_status = "fresh (%s, snapshot `%s` fetched %s)" % (
            _fmt_age(pricing_ctx["age_seconds"]), pricing_ctx["snapshot"]["snapshot_id"],
            pricing_ctx["snapshot"]["fetched_at"])
    elif pricing_ctx["status"] == "stale":
        rate_status = "**STALE** (%s, fetched %s) — see Estimated Cost Coverage" % (
            _fmt_age(pricing_ctx["age_seconds"]), pricing_ctx["snapshot"]["fetched_at"])
    elif pricing_ctx["status"] == "expired":
        rate_status = ("**TOO OLD TO PRICE** (%s, fetched %s; kept for reference only, new calls are unpriced) "
                       "— see Estimated Cost Coverage" % (
                           _fmt_age(pricing_ctx["age_seconds"]), pricing_ctx["snapshot"]["fetched_at"]))
    else:
        rate_status = "**UNAVAILABLE** — no valid official snapshot cached; see Estimated Cost Coverage"
    lines.append("| Official rate source | [GitHub Copilot models and pricing](%s) — cache %s |"
                 % (OFFICIAL_PRICING_PUBLIC_URL, rate_status))
    lines.append("")

    lines.extend(render_official_cost_section(task, official_summary, pricing_ctx))

    lines.append("## By Model")
    lines.append("")
    lines.append("| Model | Calls | Prompt | Cache-read | Cache-write | Completion | Reasoning | Total Tokens | Model-call Time | Est. USD (official) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for model, agg in sorted(task.get("by_model", {}).items(), key=lambda kv: -kv[1]["total_tokens"]):
        row = official_summary["per_model"].get(model)
        cost_str = format_official_model_cost(row) if row else "not priced"
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %d | %s | %s |" % (
            model, agg["call_count"], agg["prompt_tokens"], agg["cache_read_tokens"],
            agg.get("cache_write_tokens", 0), agg["completion_tokens"], agg["reasoning_tokens"],
            agg["total_tokens"], fmt_duration_ms(agg["duration_ms"]), cost_str))
    lines.append("")

    lines.append("## By Reasoning-Effort Intent")
    lines.append("")
    lines.append("_Effort intent is reported with its source, in priority order actually "
                  "applied per call: `measured:*` is the real per-call "
                  "`gen_ai.request.reasoning.level` telemetry from that exact model call. "
                  "`configured:*` is the session-level reasoning-effort setting in effect "
                  "at call time (real telemetry, but not call-specific). `inferred:*` is a "
                  "guess based on our custom-agent role mapping "
                  "(planner=medium, reviewer/test-reviewer/senior-coder=high, "
                  "coder=medium, tester=xhigh, discovery=low; legacy fixer=high, kept only "
                  "for historical sessions recorded before the fixer role was retired "
                  "and folded into coder/senior-coder), "
                  "applied only when the call falls inside a fully-completed subagent "
                  "invocation window, and is **not** actual observed request effort or "
                  "measured telemetry. This role-based inference cannot distinguish "
                  "coder's low-effort small/casual route. `unknown` means no "
                  "effort information was available for that call._")
    lines.append("")
    lines.append("| Effort (source:level) | Calls | Total Tokens | Reasoning Tokens (measured) |")
    lines.append("|---|---|---|---|")
    for key_, agg in sorted(task.get("by_effort", {}).items()):
        lines.append("| %s | %d | %d | %d |" % (key_, agg["call_count"], agg["total_tokens"], agg["reasoning_tokens"]))
    lines.append("")

    by_agent = task.get("by_agent", {})
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

    lines.extend(render_attribution_section(task))
    lines.extend(render_review_section(task))
    feature_lines = render_feature_delivery_section(task)
    lines.extend(feature_lines)

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
    lines.append("- The estimated USD cost uses GitHub's official published per-token rates "
                  "(fetched automatically from docs.github.com and cached for 24h), applied "
                  "per call at ingestion and recorded with the snapshot used. It is a list-price "
                  "estimate, not an invoice: plan-included AI Credits allowances are not deducted, "
                  "and models/usage shapes that cannot be priced exactly are excluded and listed "
                  "(partial lower bound), never counted as $0. Calls recorded before this pricing "
                  "existed for a task are reported as legacy (token counts only, never repriced). "
                  "If the source is unreachable the last valid snapshot is used and labeled stale, "
                  "but only until it is 7 days past its fetch time (the time this tool downloaded the "
                  "page, not a rate effective date); after that, or with no snapshot at all, new calls "
                  "are recorded as unpriced.")
    lines.append("- Some providers' telemetry may report output tokens EXCLUDING reasoning tokens "
                  "(observed for some Gemini calls, where reasoning > output). Because this cannot be "
                  "detected when reasoning <= output, every Gemini/Google-provider call that reports "
                  "reasoning tokens is conservatively left unpriced (token counts kept). For other "
                  "providers, a call with reasoning > output is unpriced; otherwise reasoning is "
                  "assumed to be included in the reported output.")
    lines.append("- Earlier versions of this report also showed a company fixed per-request charge "
                  "(request-pricing.json). That section is no longer part of the default report; the "
                  "config file and recorded model+effort data are preserved but unused.")
    lines.append("- If an OTEL file is truncated/rewound (rare), the gap is skipped with a "
                  "warning rather than being silently lost forever or risking a double count.")
    lines.append("- Model calls are attributed to agents only via explicit OTEL trace context (a chat "
                  "span's direct parent `invoke_agent` span, read in the same ingest). Calls without that "
                  "link stay unknown (with a reason) and legacy usage is never re-derived — unknown does not "
                  "mean the main session made them. Per-agent USD sums per-call estimates recorded at "
                  "ingestion; it is spend, not effectiveness, and review outcomes are never used to infer "
                  "rework or causality.")
    lines.append("- Recorded reviews are orchestrator-supplied records added with `record-review`, "
                  "not native telemetry, and recording them does not regenerate this report. A task "
                  "with no recorded reviews (including any work done before recording existed) is not "
                  "evidence of a skipped or failed review.")
    if feature_lines:
        lines.append("- Feature delivery records (`record-delivery`) and feature-usage links "
                      "(`record-feature-usage`) are explicit orchestrator-supplied records, not telemetry. "
                      "A call counts toward a feature only through an exact recorded call-key link; nothing "
                      "is assigned from timestamps, models, agent names, sessions or semantic guesses. Usage "
                      "before the call ledger started and calls without stable OTEL ids can never be split by "
                      "feature. Per-call USD values are list-price estimates frozen at ingestion, not an "
                      "invoice. Elapsed time is wall-clock between recorded start and completion (includes "
                      "idle time). Defect/regression counts include only explicitly confirmed, recorded "
                      "findings; none recorded is not evidence of no defects.")
    lines.append("")
    return "\n".join(lines)


def _md_cell(value):
    """Make a free-text value safe inside a Markdown table cell. Backslashes
    are escaped before pipes so an input like `a\\|b` cannot turn its own
    backslash into an escape that leaves a raw, cell-splitting pipe."""
    text = "" if value is None else str(value)
    return " ".join(text.split()).replace("\\", "\\\\").replace("|", "\\|")


ATTRIBUTION_REASON_LABELS = {
    ATTRIBUTION_REASON_V1_NO_LINK: ("recorded by attribution schema v1, before trace-parent joins were read "
                                    "(kept verbatim, never re-derived)"),
}
ATTRIBUTION_REASON_HINTS = {
    ATTRIBUTION_REASON_V1_NO_LINK: ("these calls were ingested before this tool read OTEL trace context; "
                                    "they stay unknown permanently (no backfill)."),
    ATTRIBUTION_REASON_NO_SUPPORTED_LINK: ("the chat spans carried no `traceId`/`parentSpanId`. The required "
                                           "join key is the chat span's `parentSpanId` equal to the `spanId` "
                                           "of an `invoke_agent` span in the same trace; check the Copilot CLI "
                                           "version/OTEL exporter."),
    ATTRIBUTION_REASON_DELTA_WITHOUT_ATTRIBUTION: ("these calls came from an ingest path that recorded no "
                                                   "attribution data; their span linkage is unknown and "
                                                   "they stay unknown permanently (no backfill)."),
    ATTRIBUTION_REASON_PARENT_NOT_OBSERVED: ("the parent span was not in the same ingest batch — typically an "
                                             "ingest while the agent turn was still running, or a session that "
                                             "ended before the agent span was exported. These calls are not "
                                             "re-linked later; ingest after the session exits to avoid this."),
    ATTRIBUTION_REASON_PARENT_NOT_AGENT: ("the direct parent was another span kind; ancestors are deliberately "
                                          "not walked, so these calls stay unknown."),
}


def _pct(part, whole):
    return "%.1f%%" % (100.0 * part / whole) if whole else "—"


def _format_agent_cost(row):
    if row["priced_calls"] == 0:
        return "not priced" if row["calls"] else "—"
    text = "$%.4f" % row["usd"]
    if row["unpriced_calls"]:
        text += " (partial: %d of %d calls)" % (row["priced_calls"], row["calls"])
    return text


def render_attribution_section(task):
    s = summarize_attribution(task)
    lines = ["## Attribution Coverage (model calls → agent invocations)", ""]
    if s["status"] == "unreadable":
        lines.append("_This task's attribution block has an unsupported or malformed schema; it is left "
                     "unchanged and not interpreted. Totals above are unaffected._")
        lines.append("")
        return lines
    lines.append("_Exclusive buckets that must add up to the Summary totals. A call is **attributed** only "
                 "through explicit OTEL trace context: its own `parentSpanId` names an `invoke_agent` span "
                 "(same `traceId`) read in the same ingest, whose `gen_ai.agent.name` identifies the agent. "
                 "Calls are never attributed from timestamps, models, session membership or agent time "
                 "windows. **Unknown is not the main session**: it means that join was not available for the "
                 "call (reason shown). Legacy usage predates attribution tracking for this task._")
    lines.append("")
    lines.append("| Bucket | Calls | % of calls | Total Tokens | % of tokens |")
    lines.append("|---|---|---|---|---|")
    total_calls, total_tokens = s["total_calls"], s["total_tokens"]
    if s["status"] == "not_started":
        legacy_label = "Legacy — recorded before attribution tracking (starts at the next ingest)"
    else:
        legacy_label = "Legacy — recorded before attribution tracking started (%s)" % (s["since"] or "unknown")
    lines.append("| %s | %d | %s | %d | %s |" % (
        legacy_label, s["legacy_calls"], _pct(s["legacy_calls"], total_calls),
        s["legacy_tokens"], _pct(s["legacy_tokens"], total_tokens)))
    for reason, row in sorted(s["unknown"].items()):
        label = ATTRIBUTION_REASON_LABELS.get(reason, reason)
        lines.append("| Unknown — %s | %d | %s | %d | %s |" % (
            _md_cell(label), row["calls"], _pct(row["calls"], total_calls),
            row["total_tokens"], _pct(row["total_tokens"], total_tokens)))
    lines.append("| Attributed — explicit OTEL trace parent (see By Agent below) | %d | %s | %d | %s |" % (
        s["attributed_calls"], _pct(s["attributed_calls"], total_calls),
        s["attributed_tokens"], _pct(s["attributed_tokens"], total_tokens)))
    lines.append("| **Total (Summary)** | %d | | %d | |" % (total_calls, total_tokens))
    lines.append("")
    if not s["reconciled"]:
        lines.append("> ⚠️ Attribution buckets do not reconcile with the Summary totals (legacy + unknown "
                     "+ attributed ≠ totals); treat this section as incomplete for this task.")
        lines.append("")

    if s["agents"]:
        official_total = summarize_official_cost(task)["totals"]
        lines.append("### By Agent (OTEL-measured via explicit trace parent)")
        lines.append("")
        lines.append("_Usage of the attributed calls only, summed from the same OTEL chat spans as the Summary "
                     "(no double counting). Invocation spans are `invoke_agent` spans of this task's sessions "
                     "read since attribution started. Est. USD sums the per-call official estimates already "
                     "recorded at ingestion (never repriced); it is spend, not a measure of an agent's "
                     "effectiveness or of rework. Self-reported `subagent.completed` tokens stay in the "
                     "separate By Custom Agent table and are not compared or mixed in here._")
        lines.append("")
        lines.append("| Agent | Type | Invocation spans | Calls | Prompt | Completion | Reasoning | Total Tokens "
                     "| Model-call Time | Models (calls) | Est. USD (official) |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for key, row in sorted(s["agents"].items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0])):
            models = ", ".join("%s (%d)" % (m, n) for m, n in sorted(row["models"].items(),
                                                                    key=lambda kv: (-kv[1], kv[0])))
            lines.append("| %s | %s | %d | %d | %d | %d | %d | %d | %s | %s | %s |" % (
                _md_cell(key), _md_cell(row["agent_type"] or "—"), row["invocation_spans"], row["calls"],
                row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"], row["total_tokens"],
                fmt_duration_ms(row["duration_ms"]), _md_cell(models or "—"), _format_agent_cost(row)))
        lines.append("")
        if s["attributed_priced_calls"]:
            residual = max(0.0, official_total["usd"] - s["attributed_usd"])
            lines.append("- Attributed estimate: $%.4f over %d priced call(s)%s. The remaining $%.4f of the "
                         "task's recorded estimate belongs to legacy/unknown calls and is not split by agent." % (
                             s["attributed_usd"], s["attributed_priced_calls"],
                             (" (%d attributed call(s) unpriced — lower bound)" % s["attributed_unpriced_calls"]
                              if s["attributed_unpriced_calls"] else ""),
                             residual))
            lines.append("")

    lines.append("### Attribution data quality")
    lines.append("")
    diagnostics = []
    if s["status"] == "not_started":
        diagnostics.append("Attribution tracking has not started for this task; all %d recorded call(s) are "
                           "legacy until the next ingest (they are never re-derived)." % total_calls)
    else:
        diagnostics.append("Coverage: %d of %d call(s) (%s) and %s of tokens attributed; %d unknown; %d legacy. "
                           "Attribution schema v%s since %s%s." % (
                               s["attributed_calls"], total_calls, _pct(s["attributed_calls"], total_calls),
                               _pct(s["attributed_tokens"], total_tokens), s["unknown_calls"],
                               s["legacy_calls"], s["schema_version"], s["since"] or "unknown",
                               ("; trace-parent joins read since %s" % s["attributed_since"]
                                if s["attributed_since"] and s["attributed_since"] != s["since"] else "")))
        for reason, row in sorted(s["unknown"].items()):
            if not row["calls"]:
                continue
            hint = ATTRIBUTION_REASON_HINTS.get(reason)
            label = ATTRIBUTION_REASON_LABELS.get(reason, reason.split(":", 1)[0])
            diagnostics.append("%d unknown call(s) — %s%s" % (
                row["calls"], _md_cell(label), (": " + hint) if hint else "."))
        if s["agents_without_attributed_calls"]:
            diagnostics.append("%d agent(s) have observed invocation spans but no attributed calls (their calls "
                               "were ingested in an earlier batch, before attribution started, or are unknown)."
                               % s["agents_without_attributed_calls"])
        if s["attributed_unpriced_calls"]:
            diagnostics.append("%d attributed call(s) have no official price (see Estimated Cost Coverage for "
                               "reasons); per-agent USD is a lower bound." % s["attributed_unpriced_calls"])
    if s["registry_invocations"]:
        diagnostics.append("The events.jsonl invocation registry below is not joined to OTEL attribution (no "
                           "shared identifier is used); its counts may differ from invocation spans.")
    for d in diagnostics:
        lines.append("- " + d)
    lines.append("")

    if s["registry_invocations"]:
        lines.append("### Observed Agent Invocations (events.jsonl registry — informational, not ownership)")
        lines.append("")
        lines.append("_Invocations seen as `subagent.started`/`subagent.completed` events, keyed by session "
                     "id + the event's own `agentId` (%d invocation(s) across %d session(s)). Being in this "
                     "registry does not attribute any model call or cost to that agent; the self-reported "
                     "By Custom Agent table above is separate and unchanged._"
                     % (s["registry_invocations"], s["registry_sessions"]))
        lines.append("")
        lines.append("| Agent | Invocations | Completed | Started only (no completion observed) | "
                     "Completed only (no start observed) | Conflicting observations |")
        lines.append("|---|---|---|---|---|---|")
        for name, row in sorted(s["registry"].items()):
            lines.append("| %s | %d | %d | %d | %d | %d |" % (
                _md_cell(name), row["invocations"], row["completed"], row["started_only"],
                row["completed_only"], row["conflicts"]))
        lines.append("")
    return lines


def _review_round_label(round_no):
    if isinstance(round_no, bool) or not isinstance(round_no, int):
        return "round %s" % _md_cell(round_no)
    if round_no == 0:
        return "broad (invocation 1 of max 4)"
    return "focused %d of 3 (invocation %d of max 4)" % (round_no, round_no + 1)


def render_review_section(task):
    reviews = task.get("reviews")
    if not isinstance(reviews, dict) or not isinstance(reviews.get("records"), dict) or not reviews["records"]:
        return []
    lines = ["## Recorded Reviews (orchestrator-supplied records — not native telemetry)", ""]
    if reviews.get("schema_version") != REVIEWS_SCHEMA_VERSION:
        lines.append("_This task's review records use an unsupported schema version; they are not "
                     "interpreted._")
        lines.append("")
        return lines
    lines.append("_Added explicitly with `copilot-task-report.py record-review`, based on the reviewer "
                 "response or a user-supplied outcome. They are not extracted from telemetry and do not "
                 "prove that the review happened. Invocation checks are exact-id lookups in the observed "
                 "registry above; nothing is inferred. A verdict of `unknown` never implies success._")
    lines.append("")
    records = [r for r in reviews["records"].values() if isinstance(r, dict)]

    def sort_key(r):
        rnd = r.get("round")
        return (str(r.get("cycle_id")), str(r.get("stage")), rnd if isinstance(rnd, int) else 99)

    records.sort(key=sort_key)
    lines.append("| Cycle | Stage | Round | Verdict | Session | Invocation | Invocation check | Provenance | Recorded |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    groups = {}
    for r in records:
        rnd = r.get("round")
        round_label = _review_round_label(rnd)
        inv = r.get("invocation_id")
        inv_cell = _md_cell(inv)
        if inv == REVIEW_UNKNOWN:
            inv_cell = "unknown (%s)" % _md_cell(r.get("invocation_unknown_reason"))
        prov = r.get("provenance") if isinstance(r.get("provenance"), dict) else {}
        prov_cell = "%s / %s: %s" % (_md_cell(prov.get("source")), _md_cell(prov.get("basis")),
                                     _md_cell(prov.get("reference")))
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            _md_cell(r.get("cycle_id")), _md_cell(r.get("stage")), round_label, _md_cell(r.get("verdict")),
            _md_cell(r.get("session_id")), inv_cell,
            observed_invocation_status(task, r.get("session_id"), inv), prov_cell, _md_cell(r.get("recorded_at"))))
        groups.setdefault((str(r.get("cycle_id")), str(r.get("stage"))), []).append(r)
    lines.append("")
    lines.append("Outcome per work cycle and stage (latest recorded round):")
    lines.append("")
    for (cycle, stage), rows in sorted(groups.items()):
        latest = rows[-1]
        verdict = latest.get("verdict")
        rnd = latest.get("round")
        used = (rnd + 1) if isinstance(rnd, int) else "?"
        if verdict == "approved":
            outcome = "approved at %s" % _review_round_label(rnd)
        elif verdict == "escalated":
            outcome = "escalated to the user at %s" % _review_round_label(rnd)
        elif verdict == "needs-fixes":
            outcome = ("open — latest verdict needs-fixes at %s; not complete" % _review_round_label(rnd)
                       if isinstance(rnd, int) and rnd < REVIEW_MAX_ROUND
                       else "needs-fixes after the last allowed round; must be escalated, not complete")
        else:
            outcome = "unknown outcome at latest recorded round (insufficient to imply success)"
        if verdict != "approved":
            earlier = [r for r in rows[:-1] if r.get("verdict") == "approved"]
            if earlier:
                # Approval is not terminal: a later round in the same cycle
                # (e.g. checking tests added after a coverage review) is the
                # current outcome, and the earlier approval no longer holds.
                outcome += "; supersedes the earlier approval at %s" % _review_round_label(
                    earlier[-1].get("round"))
        lines.append("- `%s` / %s: %s — %s of max 4 invocations recorded" % (
            _md_cell(cycle), _md_cell(stage), outcome, used))
    lines.append("")
    return lines


def task_path(task_id):
    safe = task_id.replace("/", "_")
    return os.path.join(TASKS_DIR, "%s.json" % safe)


def report_path(task_id):
    safe = task_id.replace("/", "_")
    return os.path.join(REPORTS_DIR, "%s.md" % safe)


# --------------------------------------------------------------------------
# Review lifecycle records (`record-review`)
# --------------------------------------------------------------------------
#
# Explicit, orchestrator-supplied records of reviewer / test-reviewer rounds,
# stored under task["reviews"]. They are NOT native telemetry and are never
# parsed out of agent prose: the caller states each field. Validation is
# strict (no defaults for missing fields, unknown keys rejected) and the
# bounded review policy is checked against the task's existing records:
#
# - Budget scope is (task, cycle_id, stage). A cycle_id names ONE requested
#   work item; a task report may contain many cycles. Changing session id
#   never resets a cycle's budget. The tool cannot verify that a new cycle_id
#   really is a new work item — reusing one cycle per work item is the
#   orchestrator's documented obligation.
# - round 0 is the single broad review; rounds 1..3 are focused rounds. Each
#   (cycle, stage, round) slot holds exactly one record (so no second broad
#   review), round N requires round N-1, and nothing may follow an
#   "escalated" round. "approved" is NOT terminal: a later focused round
#   (still N-1 ordered and <= 3) may check tests/fixes newly changed in the
#   same cycle after an approval (e.g. tests added after a coverage review).
#   The latest recorded round is the stage's current outcome; an earlier
#   approval followed by a later non-approved round is superseded.
# - record_id is unique per task: an identical replay is a no-op, a different
#   payload under the same id is a conflict.
#
# Recording never ingests telemetry, fetches pricing, regenerates the
# Markdown report, or touches usage/legacy/attribution data.

REVIEW_RECORD_SCHEMA = "copilot-task-report.review-record"
REVIEW_RECORD_SCHEMA_VERSION = 1
REVIEWS_SCHEMA_VERSION = 1
REVIEW_UNKNOWN = "unknown"
REVIEW_STAGES = ("reviewer", "test-reviewer")
REVIEW_VERDICTS = ("approved", "needs-fixes", "escalated", "unknown")
# Only escalation ends a stage's budget early; see the policy notes above.
REVIEW_TERMINAL_VERDICTS = ("escalated",)
REVIEW_MAX_ROUND = 3
REVIEW_PROVENANCE_SOURCES = ("orchestrator-supplied",)
REVIEW_PROVENANCE_BASES = ("agent-response", "user-supplied")
REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
REVIEW_TEXT_MAX_LEN = 500
REVIEW_INPUT_MAX_BYTES = 64 * 1024
REVIEW_INPUT_KEYS = frozenset((
    "schema", "schema_version", "record_id", "task_id", "session_id", "cycle_id", "stage", "round",
    "verdict", "invocation_id", "invocation_unknown_reason", "provenance",
))
REVIEW_PROVENANCE_KEYS = frozenset(("source", "basis", "reference"))


class ReviewRecordError(ValueError):
    """Invalid review-record input or a record that conflicts with policy/state."""


def _review_reject_constant(name):
    raise ReviewRecordError("non-finite number %r is not allowed" % name)


def _review_no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ReviewRecordError("duplicate JSON key %r" % key)
        obj[key] = value
    return obj


def _review_identifier(value, field, allow_unknown):
    if not isinstance(value, str):
        raise ReviewRecordError("%s must be a string (got %s)" % (field, type(value).__name__))
    if value.lower() == REVIEW_UNKNOWN:
        if allow_unknown and value == REVIEW_UNKNOWN:
            return value
        if allow_unknown:
            raise ReviewRecordError("%s: use the exact lowercase literal %r for an unknown value"
                                    % (field, REVIEW_UNKNOWN))
        raise ReviewRecordError("%s may not be the reserved value %r" % (field, REVIEW_UNKNOWN))
    if not REVIEW_ID_RE.match(value):
        raise ReviewRecordError(
            "%s %r is not a safe identifier (1-200 chars: letters, digits, '.', '_', ':', '-', "
            "starting with a letter or digit)%s"
            % (field, value, " or the literal 'unknown'" if allow_unknown else ""))
    return value


def _review_text(value, field):
    if not isinstance(value, str):
        raise ReviewRecordError("%s must be a string (got %s)" % (field, type(value).__name__))
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ReviewRecordError("%s must be a single line without control characters" % field)
    text = value.strip()
    if not text:
        raise ReviewRecordError("%s must not be empty" % field)
    if len(text) > REVIEW_TEXT_MAX_LEN:
        raise ReviewRecordError("%s must be at most %d characters" % (field, REVIEW_TEXT_MAX_LEN))
    return text


def _review_choice(value, field, choices):
    if not isinstance(value, str) or value not in choices:
        raise ReviewRecordError("%s must be one of %s (got %r)" % (field, ", ".join(choices), value))
    return value


def validate_review_record(raw):
    """Validate one parsed review-record document and return its normalized
    form. Every field is required (invocation_unknown_reason exactly when
    invocation_id is 'unknown'); nothing is defaulted."""
    if not isinstance(raw, dict):
        raise ReviewRecordError("the review record must be a JSON object")
    unknown_keys = sorted(set(raw) - REVIEW_INPUT_KEYS)
    if unknown_keys:
        raise ReviewRecordError("unknown field(s): %s" % ", ".join(unknown_keys))
    required = sorted(REVIEW_INPUT_KEYS - {"invocation_unknown_reason"} - set(raw))
    if required:
        raise ReviewRecordError("missing required field(s): %s" % ", ".join(required))
    if raw["schema"] != REVIEW_RECORD_SCHEMA:
        raise ReviewRecordError("schema must be %r (got %r)" % (REVIEW_RECORD_SCHEMA, raw["schema"]))
    version = raw["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != REVIEW_RECORD_SCHEMA_VERSION:
        raise ReviewRecordError("unsupported schema_version %r (this tool supports %d)"
                                % (version, REVIEW_RECORD_SCHEMA_VERSION))
    task_raw = raw["task_id"]
    if not isinstance(task_raw, str):
        raise ReviewRecordError("task_id must be a string")
    task_id = normalize_task_id(task_raw)
    if task_id is None:
        raise ReviewRecordError(
            "task_id %r is not a valid task ID/name (expected e.g. ABC-123, a free-form name using "
            "letters/digits/./_/- up to %d characters, or the literal '%s')" % (task_raw, MAX_TASK_ID_LEN, UNASSIGNED))
    rnd = raw["round"]
    if isinstance(rnd, bool) or not isinstance(rnd, int) or not 0 <= rnd <= REVIEW_MAX_ROUND:
        raise ReviewRecordError("round must be an integer 0 (broad review) to %d (focused round) (got %r)"
                                % (REVIEW_MAX_ROUND, rnd))
    invocation_id = _review_identifier(raw["invocation_id"], "invocation_id", allow_unknown=True)
    if invocation_id == REVIEW_UNKNOWN:
        if "invocation_unknown_reason" not in raw:
            raise ReviewRecordError("invocation_unknown_reason is required when invocation_id is 'unknown'")
        unknown_reason = _review_text(raw["invocation_unknown_reason"], "invocation_unknown_reason")
    else:
        if "invocation_unknown_reason" in raw:
            raise ReviewRecordError("invocation_unknown_reason is only allowed when invocation_id is 'unknown'")
        unknown_reason = None
    prov = raw["provenance"]
    if not isinstance(prov, dict):
        raise ReviewRecordError("provenance must be an object with source, basis and reference")
    prov_unknown = sorted(set(prov) - REVIEW_PROVENANCE_KEYS)
    if prov_unknown:
        raise ReviewRecordError("unknown provenance field(s): %s" % ", ".join(prov_unknown))
    prov_missing = sorted(REVIEW_PROVENANCE_KEYS - set(prov))
    if prov_missing:
        raise ReviewRecordError("missing provenance field(s): %s" % ", ".join(prov_missing))
    return {
        "record_id": _review_identifier(raw["record_id"], "record_id", allow_unknown=False),
        "task_id": task_id,
        "session_id": _review_identifier(raw["session_id"], "session_id", allow_unknown=True),
        "cycle_id": _review_identifier(raw["cycle_id"], "cycle_id", allow_unknown=False),
        "stage": _review_choice(raw["stage"], "stage", REVIEW_STAGES),
        "round": rnd,
        "verdict": _review_choice(raw["verdict"], "verdict", REVIEW_VERDICTS),
        "invocation_id": invocation_id,
        "invocation_unknown_reason": unknown_reason,
        "provenance": {
            "source": _review_choice(prov["source"], "provenance.source", REVIEW_PROVENANCE_SOURCES),
            "basis": _review_choice(prov["basis"], "provenance.basis", REVIEW_PROVENANCE_BASES),
            "reference": _review_text(prov["reference"], "provenance.reference"),
        },
    }


def load_review_record_input(path):
    """Read and validate a review-record JSON file (UTF-8, at most
    REVIEW_INPUT_MAX_BYTES, no duplicate keys or NaN/Infinity)."""
    try:
        with open(path, "rb") as f:
            data = f.read(REVIEW_INPUT_MAX_BYTES + 1)
    except OSError as ex:
        raise ReviewRecordError("cannot read input file %r: %s" % (path, ex))
    if len(data) > REVIEW_INPUT_MAX_BYTES:
        raise ReviewRecordError("input file exceeds %d bytes" % REVIEW_INPUT_MAX_BYTES)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise ReviewRecordError("input file is not valid UTF-8: %s" % ex)
    if not text.strip():
        raise ReviewRecordError("input file is empty")
    try:
        raw = json.loads(text, object_pairs_hook=_review_no_duplicate_keys,
                         parse_constant=_review_reject_constant)
    except json.JSONDecodeError as ex:
        raise ReviewRecordError("input file is not valid JSON: %s" % ex)
    return validate_review_record(raw)


def _stored_review_core(stored):
    keys = ("record_id", "task_id", "session_id", "cycle_id", "stage", "round", "verdict",
            "invocation_id", "invocation_unknown_reason", "provenance")
    return {k: stored.get(k) for k in keys}


def apply_review_record(task, record):
    """Validate `record` against the task's existing review records and add
    it. Returns "recorded" or "duplicate" (identical replay; task unchanged).
    Raises ReviewRecordError on any conflict or policy violation, leaving
    `task` unchanged."""
    reviews = task.get("reviews")
    if reviews is None:
        reviews = {"schema_version": REVIEWS_SCHEMA_VERSION, "records": {}}
    elif (not isinstance(reviews, dict) or reviews.get("schema_version") != REVIEWS_SCHEMA_VERSION
          or not isinstance(reviews.get("records"), dict)):
        raise ReviewRecordError("task '%s' has an unsupported or malformed 'reviews' block; refusing to modify it"
                                % record["task_id"])
    records = reviews["records"]

    existing = records.get(record["record_id"])
    if existing is not None:
        if isinstance(existing, dict) and _stored_review_core(existing) == record:
            return "duplicate"
        raise ReviewRecordError("record_id '%s' is already recorded for task '%s' with different content"
                                % (record["record_id"], record["task_id"]))

    same_slot = [r for r in records.values() if isinstance(r, dict)
                 and r.get("cycle_id") == record["cycle_id"] and r.get("stage") == record["stage"]]
    for r in same_slot:
        if r.get("round") == record["round"]:
            what = "a broad review (round 0)" if record["round"] == 0 else "focused round %d" % record["round"]
            raise ReviewRecordError(
                "cycle '%s' already has %s for stage %s (record '%s'); a second one is not allowed "
                "and a new session does not reset the budget" % (
                    record["cycle_id"], what, record["stage"], r.get("record_id")))
    for r in same_slot:
        if r.get("verdict") in REVIEW_TERMINAL_VERDICTS:
            raise ReviewRecordError(
                "cycle '%s' stage %s already ended with verdict '%s' at round %s; no further rounds may be "
                "recorded" % (record["cycle_id"], record["stage"], r.get("verdict"), r.get("round")))
    if record["round"] > 0 and not any(r.get("round") == record["round"] - 1 for r in same_slot):
        raise ReviewRecordError(
            "round %d for cycle '%s' stage %s requires round %d to be recorded first" % (
                record["round"], record["cycle_id"], record["stage"], record["round"] - 1))

    stored = dict(record)
    stored["provenance"] = dict(record["provenance"])
    stored["recorded_at"] = now_iso()
    records[record["record_id"]] = stored
    reviews["updated_at"] = stored["recorded_at"]
    task["reviews"] = reviews
    return "recorded"


def _load_task_file_strict(tpath, task_id):
    """Strict task-file read shared by record-review and ingest: returns
    (task_or_None, error). A missing file is a legitimate new task
    (None, None). A file that exists but is unreadable, not valid UTF-8 JSON,
    not a JSON object, or that names a different task is an error and is
    never silently replaced. The stored identity is `task_id`, else the
    pre-2.0 `jira_key`; it matches when it equals `task_id` exactly or
    normalizes to it (legacy un-normalized ids), and a file with neither key
    is accepted as before. Reading has no side effects."""
    if not os.path.exists(tpath):
        return None, None
    try:
        with open(tpath, "rb") as f:
            task = json.loads(f.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as ex:
        return None, "task file %s is unreadable (%s); refusing to overwrite it" % (tpath, ex)
    if not isinstance(task, dict):
        return None, "task file %s does not contain a JSON object; refusing to overwrite it" % tpath
    for key in ("task_id", "jira_key"):
        if key in task:
            stored = task[key]
            if stored != task_id and (not isinstance(stored, str) or normalize_task_id(stored) != task_id):
                return None, ("task file %s belongs to task %r, not '%s'; refusing to modify it"
                              % (tpath, stored, task_id))
            break
    return task, None


# --------------------------------------------------------------------------
# Feature delivery tracking: call ledger, delivery records and explicit
# feature-usage links (`record-delivery`, `feature-calls`,
# `record-feature-usage`) — additive and versioned
# --------------------------------------------------------------------------
#
# Three explicitly separated pieces, all optional (a task that never uses
# them renders exactly as before):
#
# 1. task["feature_usage"] (schema FEATURE_USAGE_SCHEMA_VERSION) — an
#    immutable per-call LEDGER filled by ingest. A model call whose OTEL chat
#    span carries valid ids is stored once under the stable key
#    (session id, traceId, the chat span's OWN spanId) with minimal data:
#    model, token counts, usage-presence flags, span duration, parent span
#    id, start time (display only), the agent label ONLY when the existing
#    attribution rule resolved it (direct invoke_agent parent read in the
#    same ingest — unchanged) and the official pricing outcome FROZEN at
#    ingestion (snapshot id, tier, component USD) or the explicit unpriced
#    reason. No prompts, completions or tool results are stored.
#    invoke_agent spans of the session are stored the same way as observed
#    invocations. Native OTEL data is untrusted: ids must match
#    OTEL_ID_RE and usage counts must be finite non-negative integers
#    (booleans/strings rejected). A call without a valid stable key is
#    counted per reason under "unledgered" — no key is ever invented. A
#    stable key seen again (e.g. the same span exported to two files) is
#    never ledgered twice: it is counted under "repeated_observations"
#    (the existing byte-offset aggregation and its idempotence are
#    unchanged, so those observations stay in the task totals). When the
#    block starts on a task, the task's existing totals and recorded cost
#    coverage are frozen as the "baseline": historical, unsplittable, never
#    rescanned or backfilled. Conservation, checked by the report:
#      totals == baseline + ledger calls + unledgered + repeated.
#
# 2. task["delivery"] (schema DELIVERY_SCHEMA_VERSION) — explicit
#    orchestrator-supplied lifecycle records per (feature_id, cycle_id):
#    exactly one "started", then at most one terminal "completed" or
#    "cancelled" (no reopen/restart, so a cycle's review budget is never
#    reset), plus confirmed material findings ("defect-confirmed" /
#    "regression-confirmed") identified by a task-unique finding_id so one
#    logical finding is never counted twice across rounds. A cycle_id
#    belongs to exactly one feature_id within the task. Late findings and
#    late usage links against an ended cycle are allowed (explicit audit;
#    the lifecycle is unchanged).
#
# 3. task["feature_usage"]["assignments"] — append-only explicit links of
#    exact ledger call keys to one started (feature_id, cycle_id). A
#    selector is an exact call key, or a direct invocation (session + trace +
#    the span id of an invoke_agent span recorded in the ledger) that
#    expands ONLY to ledger calls of the same session and trace whose own
#    parentSpanId equals that span id — never ancestors, time windows,
#    models, whole sessions, or calls ingested after the record. The
#    expanded set is fingerprinted (count + sha256 of the sorted call keys)
#    by the read-only `feature-calls` preview and re-checked when recording;
#    a changed selection is refused. Each call belongs to at most one
#    feature cycle per task: a conflicting link is refused (never moved),
#    while links overlapping calls already linked to the SAME cycle are
#    accepted and counted once (e.g. re-selecting an invocation after more
#    of its calls were ingested).
#
# Recording commands never ingest, fetch pricing or regenerate the report;
# `feature-calls` is read-only (no lock, directories, pricing or network).
# Per-call USD values are list-price estimates frozen at ingestion, not an
# invoice; nothing here accepts an externally supplied cost.

FEATURE_USAGE_SCHEMA_VERSION = 1
FEATURE_USAGE_RECORD_SCHEMA = "copilot-task-report.feature-usage-record"
FEATURE_USAGE_RECORD_SCHEMA_VERSION = 1
FEATURE_CALLS_OUTPUT_SCHEMA = "copilot-task-report.feature-calls"
FEATURE_CALLS_OUTPUT_SCHEMA_VERSION = 1
DELIVERY_RECORD_SCHEMA = "copilot-task-report.delivery-record"
DELIVERY_RECORD_SCHEMA_VERSION = 1
DELIVERY_SCHEMA_VERSION = 1

LEDGER_CALL_SOURCE = "otel-chat-span"
LEDGER_INVOCATION_SOURCE = "otel-invoke-agent-span"
LEDGER_USAGE_ATTRS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.reasoning.output_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_write.input_tokens",
)
LEDGER_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cache_read_tokens",
                     "cache_write_tokens", "total_tokens")
LEDGER_MAX_COUNT = 2 ** 53
LEDGER_MAX_LABEL_LEN = 200
# OTEL trace/span ids (hex in practice). Deliberately excludes "/" so the
# "session/trace/span" call key is unambiguous; all-zero ids are invalid.
OTEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
LEDGER_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

LEDGER_REASON_SESSION_ID = "session id is not a safe identifier, so no stable call key can be formed"
LEDGER_REASON_NO_TRACE_ID = "chat span has no valid traceId (no stable call key; never invented)"
LEDGER_REASON_NO_SPAN_ID = "chat span has no valid own spanId (no stable call key; never invented)"
LEDGER_REASON_INVALID_USAGE = ("usage attributes are not finite non-negative integers (booleans, strings and "
                               "fractions rejected)")
LEDGER_REASON_INVALID_MODEL = "model identifier is missing or not a bounded printable string"
LEDGER_REASON_DELTA_WITHOUT_LEDGER = "the ingest delta carried no call-ledger data (older code path)"

LEDGER_AGENT_LINK_RESOLVED = "direct-parent-invoke-agent-same-ingest"
LEDGER_AGENT_LINK_CODES = {
    ATTRIBUTION_REASON_NO_SUPPORTED_LINK: "no-trace-parent",
    ATTRIBUTION_REASON_PARENT_NOT_OBSERVED: "parent-not-observed-in-same-ingest",
    ATTRIBUTION_REASON_PARENT_NOT_AGENT: "parent-not-invoke-agent",
}
LEDGER_PROVENANCE = {
    "calls": ("OTEL 'chat *' spans of the session, keyed by session id + traceId + the span's own spanId; "
              "pricing frozen at ingestion"),
    "invocations": "OTEL invoke_agent spans of the session, keyed by session id + traceId + spanId",
    "baseline": "task totals and recorded official-cost coverage frozen when the ledger started (unsplittable)",
}

DELIVERY_EVENTS_LIFECYCLE = ("started", "completed", "cancelled")
DELIVERY_EVENTS_TERMINAL = ("completed", "cancelled")
DELIVERY_EVENTS_FINDING = ("defect-confirmed", "regression-confirmed")
DELIVERY_EVENTS = DELIVERY_EVENTS_LIFECYCLE + DELIVERY_EVENTS_FINDING
# Only material findings are recorded; optional polish/style is out of scope.
DELIVERY_SEVERITIES = ("critical", "high", "medium")
DELIVERY_CATEGORIES = ("correctness", "security", "safety", "data-integrity")
DELIVERY_PROVENANCE_BASES = ("user-supplied", "agent-response", "orchestrator-observed")
FEATURE_USAGE_PROVENANCE_BASES = ("user-supplied", "orchestrator-selected")
TRACKING_PROVENANCE_SOURCES = ("orchestrator-supplied",)
DELIVERY_COMMON_KEYS = ("schema", "schema_version", "record_id", "task_id", "feature_id", "cycle_id", "event",
                        "occurred_at", "provenance")
DELIVERY_FINDING_KEYS = ("finding_id", "severity", "category", "evidence_reference")
DELIVERY_REGRESSION_KEYS = ("previous_working_reference",)
DELIVERY_CORE_KEYS = ("record_id", "task_id", "feature_id", "cycle_id", "event", "occurred_at", "provenance",
                      "finding_id", "severity", "category", "evidence_reference", "previous_working_reference")
DELIVERY_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
DELIVERY_FUTURE_SKEW_SECONDS = 300
DELIVERY_INPUT_MAX_BYTES = 64 * 1024

FEATURE_USAGE_INPUT_KEYS = frozenset((
    "schema", "schema_version", "record_id", "task_id", "feature_id", "cycle_id", "selectors",
    "expected_call_count", "expected_call_set_sha256", "provenance",
))
FEATURE_USAGE_CORE_KEYS = ("record_id", "task_id", "feature_id", "cycle_id", "selectors", "expected_call_count",
                           "expected_call_set_sha256", "provenance")
FEATURE_SELECTOR_KEYS = frozenset(("kind", "session_id", "trace_id", "span_id"))
FEATURE_SELECTOR_KINDS = ("call", "direct-invocation")
FEATURE_USAGE_MAX_SELECTORS = 5000
FEATURE_USAGE_MAX_CALLS = 10 ** 6
FEATURE_USAGE_INPUT_MAX_BYTES = 1024 * 1024
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
TRACKING_PROVENANCE_KEYS = frozenset(("source", "basis", "reference"))
# Characters refused in stored free-text references (they are rendered
# inside Markdown code spans, so no HTML/link/table injection is possible).
TRACKING_REFERENCE_FORBIDDEN = frozenset("`|\\")


class TrackingRecordError(ValueError):
    """Invalid delivery / feature-usage input, or a record that conflicts
    with the task's existing state or the call ledger."""


# ---- call ledger (ingest side) -------------------------------------------

def _strict_count(value):
    """A finite, non-negative, integral usage count as int, else None.
    Booleans, strings, fractions and out-of-range values are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= LEDGER_MAX_COUNT else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and 0 <= value <= LEDGER_MAX_COUNT:
        return int(value)
    return None


def _valid_otel_id(value):
    return (isinstance(value, str) and bool(OTEL_ID_RE.match(value))
            and value.strip("0") != "")


def _valid_ledger_session_id(value):
    return isinstance(value, str) and bool(LEDGER_SESSION_ID_RE.match(value))


def _ledger_key(session_id, trace_id, span_id):
    """Stable call/invocation key. Every component is validated to exclude
    "/", so the key is unambiguous."""
    return "%s/%s/%s" % (session_id, trace_id, span_id)


def _epoch_to_iso(epoch):
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch):
        return None
    try:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def _safe_label(value):
    if not isinstance(value, str) or not value or len(value) > LEDGER_MAX_LABEL_LEN:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    return value


def _blank_ledger_bucket():
    return {"usage": blank_agg(), "priced_calls": 0, "unpriced_calls": 0, "usd": 0.0}


def _ledger_bucket_add(bucket, tokens, duration_ms, pricing):
    usage = bucket.setdefault("usage", blank_agg())
    for k in LEDGER_TOKEN_KEYS:
        usage[k] = _int_or_zero(usage.get(k)) + _int_or_zero(tokens.get(k))
    usage["call_count"] = _int_or_zero(usage.get("call_count")) + 1
    usage["duration_ms"] = _int_or_zero(usage.get("duration_ms")) + _int_or_zero(duration_ms)
    status = pricing.get("status") if isinstance(pricing, dict) else None
    if status == "priced":
        bucket["priced_calls"] = _int_or_zero(bucket.get("priced_calls")) + 1
        bucket["usd"] = _float_or_zero(bucket.get("usd")) + _float_or_zero(pricing.get("usd"))
    elif status == "unpriced":
        bucket["unpriced_calls"] = _int_or_zero(bucket.get("unpriced_calls")) + 1


def _merge_ledger_bucket(dst, src):
    usage = dst.setdefault("usage", blank_agg())
    src_usage = src.get("usage") if isinstance(src.get("usage"), dict) else {}
    for k in blank_agg():
        usage[k] = _int_or_zero(usage.get(k)) + _int_or_zero(src_usage.get(k))
    for k in ("priced_calls", "unpriced_calls"):
        dst[k] = _int_or_zero(dst.get(k)) + _int_or_zero(src.get(k))
    dst["usd"] = _float_or_zero(dst.get("usd")) + _float_or_zero(src.get("usd"))


def _ledger_call_entry(mc, session_id, ingested_at):
    """(entry, None) for a call with a valid stable key and strictly valid
    usage, else (None, unledgered_reason). `session_id` is None when the
    session id itself is not a safe identifier."""
    if session_id is None:
        return None, LEDGER_REASON_SESSION_ID
    trace_id, span_id = mc.get("trace_id"), mc.get("span_id")
    if not _valid_otel_id(trace_id):
        return None, LEDGER_REASON_NO_TRACE_ID
    if not _valid_otel_id(span_id):
        return None, LEDGER_REASON_NO_SPAN_ID
    if mc.get("usage_strict_ok") is not True:
        return None, LEDGER_REASON_INVALID_USAGE
    model = _safe_label(mc.get("model"))
    if model is None:
        return None, LEDGER_REASON_INVALID_MODEL
    tokens = {}
    for k in LEDGER_TOKEN_KEYS:
        value = mc.get(k)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2 * LEDGER_MAX_COUNT:
            return None, LEDGER_REASON_INVALID_USAGE
        tokens[k] = value
    parent = mc.get("parent_span_id")
    resolved = mc.get("attribution") if isinstance(mc.get("attribution"), dict) else {}
    if isinstance(resolved.get("agent_key"), str):
        agent = {"agent_key": resolved["agent_key"],
                 "agent_type": resolved.get("agent_type") if isinstance(resolved.get("agent_type"), str) else None}
        agent_link = LEDGER_AGENT_LINK_RESOLVED
    else:
        agent = None
        agent_link = LEDGER_AGENT_LINK_CODES.get(resolved.get("reason"), "not-resolved")
    pricing = mc.get("official_pricing")
    if isinstance(pricing, dict) and pricing.get("status") == "priced":
        frozen = {
            "status": "priced",
            "snapshot_id": pricing.get("snapshot_id"),
            "official_model": pricing.get("official_model"),
            "tier": pricing.get("tier"),
            "usd": _float_or_zero(pricing.get("usd")),
            "components": {k: _float_or_zero(v) for k, v in (pricing.get("components") or {}).items()},
        }
    elif isinstance(pricing, dict) and pricing.get("status") == "unpriced":
        frozen = {"status": "unpriced", "reason": str(pricing.get("reason"))}
    else:
        frozen = {"status": "not-recorded",
                  "reason": "no official pricing result was recorded for this call at ingestion"}
    present = mc.get("usage_present") if isinstance(mc.get("usage_present"), dict) else {}
    entry = {
        "session_id": session_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent if _valid_otel_id(parent) else None,
        "started_at": _epoch_to_iso(mc.get("start_ts")),
        "model": model,
        "usage": tokens,
        "usage_present": {k: bool(v) for k, v in present.items()},
        "duration_ms": _int_or_zero(mc.get("duration_ms")),
        "agent": agent,
        "agent_link": agent_link,
        "pricing": frozen,
        "source": LEDGER_CALL_SOURCE,
        "ingested_at": ingested_at,
    }
    return entry, None


def _ledger_invocation_entry(info, session_id, ingested_at):
    if session_id is None:
        return None
    trace_id, span_id = info.get("trace_id"), info.get("span_id")
    if not _valid_otel_id(trace_id) or not _valid_otel_id(span_id):
        return None
    parent = info.get("parent_span_id")
    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent if _valid_otel_id(parent) else None,
        "agent_key": info.get("agent_key") if isinstance(info.get("agent_key"), str) else None,
        "agent_type": info.get("agent_type") if isinstance(info.get("agent_type"), str) else None,
        "started_at": _epoch_to_iso(info.get("start_ts")),
        "source": LEDGER_INVOCATION_SOURCE,
        "ingested_at": ingested_at,
    }


def build_feature_ledger_delta(session_id, calls, agent_spans):
    """Ledger delta for one ingest batch (see the section notes above)."""
    out = {"calls": [], "unledgered": {}, "invocations": [], "invocations_without_stable_ids": 0}
    sid = session_id if _valid_ledger_session_id(session_id) else None
    ingested_at = now_iso()
    for mc in calls:
        entry, reason = _ledger_call_entry(mc, sid, ingested_at)
        if entry is not None:
            out["calls"].append(entry)
            continue
        bucket = out["unledgered"].setdefault(reason, _blank_ledger_bucket())
        _ledger_bucket_add(bucket, mc, mc.get("duration_ms"), mc.get("official_pricing"))
    for info in agent_spans:
        inv = _ledger_invocation_entry(info, sid, ingested_at)
        if inv is None:
            out["invocations_without_stable_ids"] += 1
        else:
            out["invocations"].append(inv)
    return out


def _fallback_feature_ledger_delta(delta):
    """Unledgered booking for a delta built without ledger data: usage from
    its by_model aggregates and cost coverage from its recorded official-cost
    buckets (so conservation with the merged totals still holds)."""
    bucket = _blank_ledger_bucket()
    for agg in (delta.get("by_model") or {}).values():
        for k in blank_agg():
            bucket["usage"][k] += _int_or_zero(agg.get(k))
    oc = delta.get("official_cost") or {}
    for rec in (oc.get("by_model") or {}).values():
        for b in (rec.get("priced") or {}).values():
            bucket["priced_calls"] += _int_or_zero(b.get("calls"))
            bucket["usd"] += _float_or_zero(b.get("usd"))
        for u in (rec.get("unpriced") or {}).values():
            bucket["unpriced_calls"] += _int_or_zero(u.get("calls"))
    unledgered = {LEDGER_REASON_DELTA_WITHOUT_LEDGER: bucket} if bucket["usage"]["call_count"] else {}
    return {"calls": [], "unledgered": unledgered, "invocations": [], "invocations_without_stable_ids": 0}


def _feature_usage_shape_ok(fu):
    if not (isinstance(fu, dict) and fu.get("schema_version") == FEATURE_USAGE_SCHEMA_VERSION):
        return False
    baseline = fu.get("baseline")
    if not (isinstance(baseline, dict) and isinstance(baseline.get("usage"), dict)
            and isinstance(baseline.get("official_cost"), dict)):
        return False
    return all(isinstance(fu.get(k), dict) for k in
               ("calls", "invocations", "unledgered", "repeated_observations", "assignments"))


def prepare_feature_usage_block(task):
    """Return the task's feature-usage block, starting it (with the frozen
    pre-ledger baseline) if absent. Must run before the current delta is
    merged. An unsupported/malformed block is left untouched (None)."""
    fu = task.get("feature_usage")
    if fu is None:
        prior = task.get("totals") if isinstance(task.get("totals"), dict) else {}
        oc = summarize_official_cost(task)["totals"]
        fu = task["feature_usage"] = {
            "schema_version": FEATURE_USAGE_SCHEMA_VERSION,
            "since": now_iso(),
            "baseline": {
                "usage": {k: _int_or_zero(prior.get(k)) for k in blank_agg()},
                "official_cost": {
                    "priced_calls": _int_or_zero(oc.get("priced_calls")),
                    "unpriced_calls": _int_or_zero(oc.get("unpriced_calls")),
                    "legacy_calls": _int_or_zero(oc.get("legacy_calls")),
                    "usd": _float_or_zero(oc.get("usd")),
                },
            },
            "calls": {},
            "invocations": {},
            "unledgered": {},
            "repeated_observations": dict(_blank_ledger_bucket(), conflicting=0),
            "repeated_invocation_observations": 0,
            "invocations_without_stable_ids": 0,
            "assignments": {},
            "provenance": dict(LEDGER_PROVENANCE),
        }
        return fu
    if _feature_usage_shape_ok(fu):
        return fu
    print("Warning: task '%s' has an unsupported/malformed feature_usage block; leaving it unchanged "
          "(new calls are still counted in the task totals but not ledgered)." % task.get("task_id", "?"),
          file=sys.stderr)
    return None


def _ledger_observation_core(entry):
    return (entry.get("model"), entry.get("usage"), entry.get("parent_span_id"), entry.get("started_at"),
            entry.get("duration_ms"))


def merge_feature_ledger_delta(fu, ledger_delta):
    """Merge one ingest's ledger delta. Existing entries are never modified;
    a repeated stable key is counted once in repeated_observations (and as
    conflicting when its observed data differs) instead of being ledgered
    twice."""
    if fu is None or not ledger_delta:
        return
    calls = fu["calls"]
    repeated = fu["repeated_observations"]
    for entry in ledger_delta.get("calls") or []:
        key = _ledger_key(entry["session_id"], entry["trace_id"], entry["span_id"])
        existing = calls.get(key)
        if existing is None:
            calls[key] = entry
            continue
        _ledger_bucket_add(repeated, entry["usage"], entry.get("duration_ms"), entry.get("pricing"))
        if not isinstance(existing, dict) or _ledger_observation_core(existing) != _ledger_observation_core(entry):
            repeated["conflicting"] = _int_or_zero(repeated.get("conflicting")) + 1
    for reason, bucket in (ledger_delta.get("unledgered") or {}).items():
        dst = fu["unledgered"].get(reason)
        if not isinstance(dst, dict):
            dst = fu["unledgered"][reason] = _blank_ledger_bucket()
        _merge_ledger_bucket(dst, bucket)
    invocations = fu["invocations"]
    for inv in ledger_delta.get("invocations") or []:
        key = _ledger_key(inv["session_id"], inv["trace_id"], inv["span_id"])
        if key in invocations:
            fu["repeated_invocation_observations"] = _int_or_zero(fu.get("repeated_invocation_observations")) + 1
        else:
            invocations[key] = inv
    missing = _int_or_zero(ledger_delta.get("invocations_without_stable_ids"))
    if missing:
        fu["invocations_without_stable_ids"] = _int_or_zero(fu.get("invocations_without_stable_ids")) + missing


# ---- shared strict input helpers -----------------------------------------

def _tracking_identifier(value, field):
    try:
        return _review_identifier(value, field, allow_unknown=False)
    except ReviewRecordError as ex:
        raise TrackingRecordError(str(ex))


def _tracking_reference(value, field):
    try:
        text = _review_text(value, field)
    except ReviewRecordError as ex:
        raise TrackingRecordError(str(ex))
    if TRACKING_REFERENCE_FORBIDDEN & set(text):
        raise TrackingRecordError("%s must not contain backticks, pipes or backslashes (store a short "
                                  "reference such as a PR/test/finding pointer, not prose)" % field)
    return text


def _tracking_choice(value, field, choices):
    if not isinstance(value, str) or value not in choices:
        raise TrackingRecordError("%s must be one of %s (got %r)" % (field, ", ".join(choices), value))
    return value


def _tracking_task_id(value):
    if not isinstance(value, str):
        raise TrackingRecordError("task_id must be a string")
    task_id = normalize_task_id(value)
    if task_id is None:
        raise TrackingRecordError(
            "task_id %r is not a valid task ID/name (expected e.g. ABC-123, a free-form name using "
            "letters/digits/./_/- up to %d characters, or the literal '%s')" % (value, MAX_TASK_ID_LEN, UNASSIGNED))
    return task_id


def _tracking_header(raw, schema, version, allowed_keys, required_keys, what):
    if not isinstance(raw, dict):
        raise TrackingRecordError("the %s must be a JSON object" % what)
    unknown = sorted(set(raw) - set(allowed_keys))
    if unknown:
        raise TrackingRecordError("unknown field(s): %s" % ", ".join(unknown))
    missing = sorted(set(required_keys) - set(raw))
    if missing:
        raise TrackingRecordError("missing required field(s): %s" % ", ".join(missing))
    if raw["schema"] != schema:
        raise TrackingRecordError("schema must be %r (got %r)" % (schema, raw["schema"]))
    got = raw["schema_version"]
    if isinstance(got, bool) or not isinstance(got, int) or got != version:
        raise TrackingRecordError("unsupported schema_version %r (this tool supports %d)" % (got, version))


def _tracking_provenance(prov, bases):
    if not isinstance(prov, dict):
        raise TrackingRecordError("provenance must be an object with source, basis and reference")
    unknown = sorted(set(prov) - TRACKING_PROVENANCE_KEYS)
    if unknown:
        raise TrackingRecordError("unknown provenance field(s): %s" % ", ".join(unknown))
    missing = sorted(TRACKING_PROVENANCE_KEYS - set(prov))
    if missing:
        raise TrackingRecordError("missing provenance field(s): %s" % ", ".join(missing))
    return {
        "source": _tracking_choice(prov["source"], "provenance.source", TRACKING_PROVENANCE_SOURCES),
        "basis": _tracking_choice(prov["basis"], "provenance.basis", bases),
        "reference": _tracking_reference(prov["reference"], "provenance.reference"),
    }


def _load_tracking_input(path, max_bytes):
    """Strictly read one JSON input file (UTF-8, bounded size, no duplicate
    keys, no NaN/Infinity literals)."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as ex:
        raise TrackingRecordError("cannot read input file %r: %s" % (path, ex))
    if len(data) > max_bytes:
        raise TrackingRecordError("input file exceeds %d bytes" % max_bytes)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise TrackingRecordError("input file is not valid UTF-8: %s" % ex)
    if not text.strip():
        raise TrackingRecordError("input file is empty")
    try:
        return json.loads(text, object_pairs_hook=_review_no_duplicate_keys,
                          parse_constant=_review_reject_constant)
    except ReviewRecordError as ex:
        raise TrackingRecordError(str(ex))
    except json.JSONDecodeError as ex:
        raise TrackingRecordError("input file is not valid JSON: %s" % ex)


def _utc_ts_epoch(value):
    """Epoch for a strict UTC timestamp 'YYYY-MM-DDTHH:MM:SS[.ffffff]Z', else None."""
    if not isinstance(value, str) or not DELIVERY_TS_RE.match(value):
        return None
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    fraction = value[19:-1]
    return dt.timestamp() + (float(fraction) if fraction else 0.0)


# ---- delivery records (`record-delivery`) ---------------------------------

def validate_delivery_record(raw, now=None):
    """Validate one parsed delivery-record document and return its normalized
    form (every DELIVERY_CORE_KEYS key present; finding fields None for
    lifecycle events). Nothing is defaulted."""
    if isinstance(raw, dict) and raw.get("event") in DELIVERY_EVENTS_FINDING:
        extra = DELIVERY_FINDING_KEYS + (DELIVERY_REGRESSION_KEYS if raw["event"] == "regression-confirmed" else ())
    else:
        extra = ()
    allowed = DELIVERY_COMMON_KEYS + extra
    _tracking_header(raw, DELIVERY_RECORD_SCHEMA, DELIVERY_RECORD_SCHEMA_VERSION, allowed, allowed,
                     "delivery record")
    event = _tracking_choice(raw["event"], "event", DELIVERY_EVENTS)
    occurred_at = raw["occurred_at"]
    epoch = _utc_ts_epoch(occurred_at)
    if epoch is None:
        raise TrackingRecordError("occurred_at must be a UTC timestamp like 2026-11-17T14:05:00Z (got %r)"
                                  % (occurred_at,))
    now = time.time() if now is None else now
    if epoch > now + DELIVERY_FUTURE_SKEW_SECONDS:
        raise TrackingRecordError("occurred_at %s is in the future" % occurred_at)
    record = {
        "record_id": _tracking_identifier(raw["record_id"], "record_id"),
        "task_id": _tracking_task_id(raw["task_id"]),
        "feature_id": _tracking_identifier(raw["feature_id"], "feature_id"),
        "cycle_id": _tracking_identifier(raw["cycle_id"], "cycle_id"),
        "event": event,
        "occurred_at": occurred_at,
        "provenance": _tracking_provenance(raw["provenance"], DELIVERY_PROVENANCE_BASES),
        "finding_id": None,
        "severity": None,
        "category": None,
        "evidence_reference": None,
        "previous_working_reference": None,
    }
    if event in DELIVERY_EVENTS_FINDING:
        record["finding_id"] = _tracking_identifier(raw["finding_id"], "finding_id")
        record["severity"] = _tracking_choice(raw["severity"], "severity", DELIVERY_SEVERITIES)
        record["category"] = _tracking_choice(raw["category"], "category", DELIVERY_CATEGORIES)
        record["evidence_reference"] = _tracking_reference(raw["evidence_reference"], "evidence_reference")
        if event == "regression-confirmed":
            record["previous_working_reference"] = _tracking_reference(
                raw["previous_working_reference"], "previous_working_reference")
    return record


def load_delivery_record_input(path):
    return validate_delivery_record(_load_tracking_input(path, DELIVERY_INPUT_MAX_BYTES))


def _delivery_shape_ok(block):
    return (isinstance(block, dict) and block.get("schema_version") == DELIVERY_SCHEMA_VERSION
            and isinstance(block.get("records"), dict))


def _delivery_cycles(records):
    """{cycle_id: {feature_id, started, terminal, defects, regressions}} from
    stored delivery records, in recording order."""
    cycles = {}
    for r in records.values():
        if (not isinstance(r, dict) or not isinstance(r.get("cycle_id"), str)
                or not isinstance(r.get("feature_id"), str)):
            continue
        c = cycles.setdefault(r["cycle_id"], {"feature_id": r.get("feature_id"), "started": None,
                                              "terminal": None, "defects": [], "regressions": [],
                                              "feature_conflict": False})
        if r.get("feature_id") != c["feature_id"]:
            c["feature_conflict"] = True
        event = r.get("event")
        if event == "started" and c["started"] is None:
            c["started"] = r
        elif event in DELIVERY_EVENTS_TERMINAL and c["terminal"] is None:
            c["terminal"] = r
        elif event == "defect-confirmed":
            c["defects"].append(r)
        elif event == "regression-confirmed":
            c["regressions"].append(r)
    return cycles


def apply_delivery_record(task, record):
    """Check `record` against the task's delivery records and add it.
    Returns "recorded" or "duplicate" (identical replay; task unchanged).
    Raises TrackingRecordError on any conflict, leaving `task` unchanged."""
    block = task.get("delivery")
    if block is None:
        block = {"schema_version": DELIVERY_SCHEMA_VERSION, "records": {}}
    elif not _delivery_shape_ok(block):
        raise TrackingRecordError("task '%s' has an unsupported or malformed 'delivery' block; refusing to "
                                  "modify it" % record["task_id"])
    records = block["records"]
    existing = records.get(record["record_id"])
    if existing is not None:
        if isinstance(existing, dict) and {k: existing.get(k) for k in DELIVERY_CORE_KEYS} == record:
            return "duplicate"
        raise TrackingRecordError("record_id '%s' is already recorded for task '%s' with different content"
                                  % (record["record_id"], record["task_id"]))
    cycle_id, feature_id, event = record["cycle_id"], record["feature_id"], record["event"]
    cycle = _delivery_cycles(records).get(cycle_id)
    if cycle is not None and cycle["feature_id"] != feature_id:
        raise TrackingRecordError("cycle '%s' belongs to feature '%s', not '%s'; a cycle id names one work item "
                                  "of one feature within the task" % (cycle_id, cycle["feature_id"], feature_id))
    if event == "started":
        if cycle is not None and cycle["started"] is not None:
            raise TrackingRecordError(
                "cycle '%s' was already started (record '%s'); a cycle starts once — no restart, reopen or "
                "review-budget reset" % (cycle_id, cycle["started"].get("record_id")))
    else:
        if cycle is None or cycle["started"] is None:
            raise TrackingRecordError("cycle '%s' has no 'started' record; record the start first" % cycle_id)
        start_epoch = _utc_ts_epoch(cycle["started"].get("occurred_at"))
        if start_epoch is None:
            raise TrackingRecordError("the stored start of cycle '%s' has an unreadable timestamp" % cycle_id)
        if _utc_ts_epoch(record["occurred_at"]) < start_epoch:
            raise TrackingRecordError("occurred_at %s precedes the start of cycle '%s' (%s)" % (
                record["occurred_at"], cycle_id, cycle["started"].get("occurred_at")))
        if event in DELIVERY_EVENTS_TERMINAL:
            if cycle["terminal"] is not None:
                raise TrackingRecordError(
                    "cycle '%s' already ended as '%s' (record '%s'); it cannot be completed, cancelled or "
                    "reopened again" % (cycle_id, cycle["terminal"].get("event"), cycle["terminal"].get("record_id")))
        else:
            for r in records.values():
                if isinstance(r, dict) and r.get("finding_id") == record["finding_id"]:
                    raise TrackingRecordError(
                        "finding '%s' is already recorded (record '%s', cycle '%s'); a logical finding is counted "
                        "once — do not re-record it in later rounds" % (
                            record["finding_id"], r.get("record_id"), r.get("cycle_id")))
    stored = dict(record)
    stored["provenance"] = dict(record["provenance"])
    stored["recorded_at"] = now_iso()
    records[record["record_id"]] = stored
    block["updated_at"] = stored["recorded_at"]
    task["delivery"] = block
    return "recorded"


# ---- feature-usage links (`feature-calls`, `record-feature-usage`) --------

def feature_call_set_sha256(call_keys):
    """sha256 (hex) of the UTF-8 text formed by the sorted, de-duplicated
    call keys ("session/trace/span"), each followed by a newline."""
    text = "".join("%s\n" % k for k in sorted(set(call_keys)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_selector(sel, index):
    where = "selectors[%d]" % index
    if not isinstance(sel, dict):
        raise TrackingRecordError("%s must be an object" % where)
    unknown = sorted(set(sel) - FEATURE_SELECTOR_KEYS)
    if unknown:
        raise TrackingRecordError("%s: unknown field(s): %s" % (where, ", ".join(unknown)))
    missing = sorted(FEATURE_SELECTOR_KEYS - set(sel))
    if missing:
        raise TrackingRecordError("%s: missing field(s): %s" % (where, ", ".join(missing)))
    kind = _tracking_choice(sel["kind"], where + ".kind", FEATURE_SELECTOR_KINDS)
    if not _valid_ledger_session_id(sel["session_id"]):
        raise TrackingRecordError("%s.session_id %r is not a valid session id" % (where, sel["session_id"]))
    for field in ("trace_id", "span_id"):
        if not _valid_otel_id(sel[field]):
            raise TrackingRecordError("%s.%s %r is not a valid OTEL id" % (where, field, sel[field]))
    return {"kind": kind, "session_id": sel["session_id"], "trace_id": sel["trace_id"], "span_id": sel["span_id"]}


def _validate_selectors(raw_selectors):
    if not isinstance(raw_selectors, list) or not raw_selectors:
        raise TrackingRecordError("selectors must be a non-empty list")
    if len(raw_selectors) > FEATURE_USAGE_MAX_SELECTORS:
        raise TrackingRecordError("at most %d selectors are allowed per record" % FEATURE_USAGE_MAX_SELECTORS)
    out, seen = [], set()
    for i, sel in enumerate(raw_selectors):
        norm = _validate_selector(sel, i)
        ident = (norm["kind"], norm["session_id"], norm["trace_id"], norm["span_id"])
        if ident in seen:
            raise TrackingRecordError("selectors[%d] duplicates an earlier selector" % i)
        seen.add(ident)
        out.append(norm)
    return out


def validate_feature_usage_record(raw):
    _tracking_header(raw, FEATURE_USAGE_RECORD_SCHEMA, FEATURE_USAGE_RECORD_SCHEMA_VERSION,
                     FEATURE_USAGE_INPUT_KEYS, FEATURE_USAGE_INPUT_KEYS, "feature-usage record")
    count = raw["expected_call_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= FEATURE_USAGE_MAX_CALLS:
        raise TrackingRecordError("expected_call_count must be an integer from 1 to %d (got %r)"
                                  % (FEATURE_USAGE_MAX_CALLS, count))
    digest = raw["expected_call_set_sha256"]
    if not isinstance(digest, str) or not SHA256_HEX_RE.match(digest):
        raise TrackingRecordError("expected_call_set_sha256 must be 64 lowercase hex characters")
    return {
        "record_id": _tracking_identifier(raw["record_id"], "record_id"),
        "task_id": _tracking_task_id(raw["task_id"]),
        "feature_id": _tracking_identifier(raw["feature_id"], "feature_id"),
        "cycle_id": _tracking_identifier(raw["cycle_id"], "cycle_id"),
        "selectors": _validate_selectors(raw["selectors"]),
        "expected_call_count": count,
        "expected_call_set_sha256": digest,
        "provenance": _tracking_provenance(raw["provenance"], FEATURE_USAGE_PROVENANCE_BASES),
    }


def load_feature_usage_record_input(path):
    return validate_feature_usage_record(_load_tracking_input(path, FEATURE_USAGE_INPUT_MAX_BYTES))


def _readable_feature_usage(task):
    fu = task.get("feature_usage")
    if fu is None:
        raise TrackingRecordError("task '%s' has no call ledger yet (it starts at the next ingest of a session "
                                  "of this task)" % task.get("task_id", "?"))
    if not _feature_usage_shape_ok(fu):
        raise TrackingRecordError("task '%s' has an unsupported or malformed 'feature_usage' block; refusing "
                                  "to use it" % task.get("task_id", "?"))
    return fu


def _require_started_cycle(task, feature_id, cycle_id):
    block = task.get("delivery")
    if block is None:
        raise TrackingRecordError("cycle '%s' has no 'started' delivery record; record-delivery the start first"
                                  % cycle_id)
    if not _delivery_shape_ok(block):
        raise TrackingRecordError("task '%s' has an unsupported or malformed 'delivery' block"
                                  % task.get("task_id", "?"))
    cycle = _delivery_cycles(block["records"]).get(cycle_id)
    if cycle is None or cycle["started"] is None:
        raise TrackingRecordError("cycle '%s' has no 'started' delivery record; record-delivery the start first"
                                  % cycle_id)
    if cycle["feature_id"] != feature_id:
        raise TrackingRecordError("cycle '%s' belongs to feature '%s', not '%s'"
                                  % (cycle_id, cycle["feature_id"], feature_id))
    return cycle


def _invocation_children_index(fu):
    """{(session_id, trace_id, parent_span_id): [call keys]} over the ledger:
    a call is indexed ONLY under its own direct parent span id."""
    index = {}
    for k, c in fu["calls"].items():
        if isinstance(c, dict) and c.get("parent_span_id"):
            index.setdefault((c.get("session_id"), c.get("trace_id"), c.get("parent_span_id")), []).append(k)
    return index


def expand_feature_selectors(fu, selectors):
    """Sorted exact call keys selected by validated `selectors`. Every
    selector must resolve to ledgered data and no call may be selected by
    two selectors. A direct-invocation selector expands only to ledger calls
    of the same session and trace whose own parentSpanId is the selected
    invoke_agent span id (no ancestors, time windows or later calls)."""
    owner = {}
    children = None
    for i, sel in enumerate(selectors):
        key = _ledger_key(sel["session_id"], sel["trace_id"], sel["span_id"])
        if sel["kind"] == "call":
            if not isinstance(fu["calls"].get(key), dict):
                raise TrackingRecordError("selectors[%d]: call %s is not in this task's call ledger" % (i, key))
            matched = [key]
        else:
            if not isinstance(fu["invocations"].get(key), dict):
                raise TrackingRecordError("selectors[%d]: invoke_agent span %s is not in this task's ledger of "
                                          "observed invocations" % (i, key))
            if children is None:
                children = _invocation_children_index(fu)
            matched = sorted(children.get((sel["session_id"], sel["trace_id"], sel["span_id"]), []))
            if not matched:
                raise TrackingRecordError("selectors[%d]: invocation %s has no ledgered direct child calls"
                                          % (i, key))
        for k in matched:
            if k in owner:
                raise TrackingRecordError("selectors[%d] and selectors[%d] both select call %s; select each call "
                                          "exactly once" % (owner[k], i, k))
            owner[k] = i
    if not owner:
        raise TrackingRecordError("the selection is empty")
    return sorted(owner)


def current_call_assignments(fu):
    """({call_key: (feature_id, cycle_id, record_id)}, conflicting_keys) from
    the append-only assignment records, in recording order. Conflicting
    keys (only possible in a hand-edited file) are excluded everywhere."""
    owners, conflicts = {}, set()
    for rid, rec in fu["assignments"].items():
        if (not isinstance(rec, dict) or not isinstance(rec.get("call_keys"), list)
                or not isinstance(rec.get("feature_id"), str) or not isinstance(rec.get("cycle_id"), str)):
            continue
        target = (rec.get("feature_id"), rec.get("cycle_id"))
        for k in rec["call_keys"]:
            if not isinstance(k, str):
                continue
            cur = owners.get(k)
            if cur is None:
                owners[k] = (target[0], target[1], rid)
            elif (cur[0], cur[1]) != target:
                conflicts.add(k)
    return owners, conflicts


def _check_assignment_conflicts(fu, call_keys, feature_id, cycle_id):
    """Refuse any call already linked to another feature cycle; return the
    number of calls already linked to this same cycle (counted once)."""
    owners, conflicts = current_call_assignments(fu)
    already = 0
    for k in call_keys:
        if k in conflicts:
            raise TrackingRecordError("call %s has conflicting stored links; refusing to link it" % k)
        cur = owners.get(k)
        if cur is None:
            continue
        if (cur[0], cur[1]) != (feature_id, cycle_id):
            raise TrackingRecordError(
                "call %s is already linked to feature '%s' cycle '%s' (record '%s'); links are never moved or "
                "shared — select the calls of this work explicitly" % (k, cur[0], cur[1], cur[2]))
        already += 1
    return already


def apply_feature_usage_record(task, record):
    """Validate `record` against the ledger, delivery records and existing
    links, and append it. Returns "recorded" or "duplicate"; raises
    TrackingRecordError (task unchanged) otherwise."""
    fu = _readable_feature_usage(task)
    assignments = fu["assignments"]
    existing = assignments.get(record["record_id"])
    if existing is not None:
        if isinstance(existing, dict) and {k: existing.get(k) for k in FEATURE_USAGE_CORE_KEYS} == record:
            return "duplicate"
        raise TrackingRecordError("record_id '%s' is already recorded for task '%s' with different content"
                                  % (record["record_id"], record["task_id"]))
    _require_started_cycle(task, record["feature_id"], record["cycle_id"])
    call_keys = expand_feature_selectors(fu, record["selectors"])
    digest = feature_call_set_sha256(call_keys)
    if len(call_keys) != record["expected_call_count"] or digest != record["expected_call_set_sha256"]:
        raise TrackingRecordError(
            "the selection changed since the preview: it now expands to %d call(s) with sha256 %s (expected %d, "
            "%s); re-run feature-calls and review the new selection" % (
                len(call_keys), digest, record["expected_call_count"], record["expected_call_set_sha256"]))
    already = _check_assignment_conflicts(fu, call_keys, record["feature_id"], record["cycle_id"])
    stored = dict(record)
    stored["selectors"] = [dict(s) for s in record["selectors"]]
    stored["provenance"] = dict(record["provenance"])
    stored["call_keys"] = call_keys
    stored["newly_linked_call_count"] = len(call_keys) - already
    stored["recorded_at"] = now_iso()
    assignments[record["record_id"]] = stored
    fu["assignments_updated_at"] = stored["recorded_at"]
    return "recorded"


def _pricing_view(pricing):
    if not isinstance(pricing, dict):
        return {"status": "not-recorded"}
    if pricing.get("status") == "priced":
        return {"status": "priced", "usd": _float_or_zero(pricing.get("usd")),
                "snapshot_id": pricing.get("snapshot_id"), "tier": pricing.get("tier"),
                "official_model": pricing.get("official_model")}
    return {"status": pricing.get("status") or "not-recorded", "reason": pricing.get("reason")}


def feature_calls_listing(task, task_id, session_id):
    """Read-only JSON view of one session's ledger calls and invocation
    groups with their exact selectors and current links."""
    out = {
        "schema": FEATURE_CALLS_OUTPUT_SCHEMA,
        "schema_version": FEATURE_CALLS_OUTPUT_SCHEMA_VERSION,
        "task_id": task_id,
        "session_id": session_id,
        "ledger_status": "active",
        "ledger_since": None,
        "calls": [],
        "invocations": [],
        "calls_without_ledgered_parent_invocation": 0,
        "task_unledgered_calls": {},
        "task_repeated_key_observations": None,
        "notes": [
            "Selectors are exact ids: --call TRACE/SPAN selects one call; --invocation TRACE/SPAN selects only "
            "the ledgered calls whose own parentSpanId is that invoke_agent span (direct children, same session "
            "and trace) at preview time.",
            "If one invocation served several features, select its calls individually with --call.",
            "USD values are list-price estimates frozen at ingestion, not an invoice; unpriced is not $0.",
        ],
    }
    fu = task.get("feature_usage")
    if fu is None:
        out["ledger_status"] = "not_started"
        return out
    if not _feature_usage_shape_ok(fu):
        out["ledger_status"] = "unreadable"
        return out
    out["ledger_since"] = fu.get("since")
    owners, conflicts = current_call_assignments(fu)
    session_calls = sorted(((k, c) for k, c in fu["calls"].items()
                            if isinstance(c, dict) and c.get("session_id") == session_id),
                           key=lambda kv: (str(kv[1].get("started_at") or ""), kv[0]))
    invocations = {k: inv for k, inv in fu["invocations"].items()
                   if isinstance(inv, dict) and inv.get("session_id") == session_id}
    groups = {}
    for key, c in session_calls:
        owner = owners.get(key)
        link = None
        if key in conflicts:
            link = {"status": "conflicting-stored-links"}
        elif owner is not None:
            link = {"feature_id": owner[0], "cycle_id": owner[1], "record_id": owner[2]}
        usage = c.get("usage") if isinstance(c.get("usage"), dict) else {}
        out["calls"].append({
            "selector": "%s/%s" % (c.get("trace_id"), c.get("span_id")),
            "trace_id": c.get("trace_id"),
            "span_id": c.get("span_id"),
            "parent_span_id": c.get("parent_span_id"),
            "started_at": c.get("started_at"),
            "model": c.get("model"),
            "agent": c.get("agent"),
            "agent_link": c.get("agent_link"),
            "duration_ms": _int_or_zero(c.get("duration_ms")),
            "total_tokens": _int_or_zero(usage.get("total_tokens")),
            "pricing": _pricing_view(c.get("pricing")),
            "link": link,
        })
        parent_key = _ledger_key(session_id, c.get("trace_id"), c.get("parent_span_id"))
        if c.get("parent_span_id") and parent_key in invocations:
            groups.setdefault(parent_key, []).append((key, link))
        else:
            out["calls_without_ledgered_parent_invocation"] += 1
    for key, inv in sorted(invocations.items(), key=lambda kv: (str(kv[1].get("started_at") or ""), kv[0])):
        members = groups.get(key, [])
        linked = {}
        for _k, link in members:
            if link and "cycle_id" in link:
                label = "%s/%s" % (link["feature_id"], link["cycle_id"])
                linked[label] = linked.get(label, 0) + 1
        out["invocations"].append({
            "selector": "%s/%s" % (inv.get("trace_id"), inv.get("span_id")),
            "trace_id": inv.get("trace_id"),
            "span_id": inv.get("span_id"),
            "parent_span_id": inv.get("parent_span_id"),
            "started_at": inv.get("started_at"),
            "agent_key": inv.get("agent_key"),
            "agent_type": inv.get("agent_type"),
            "direct_calls": len(members),
            "unlinked_direct_calls": sum(1 for _k, link in members if link is None),
            "linked_direct_calls": linked,
        })
    for reason, bucket in fu["unledgered"].items():
        if isinstance(bucket, dict):
            usage = bucket.get("usage") if isinstance(bucket.get("usage"), dict) else {}
            out["task_unledgered_calls"][reason] = _int_or_zero(usage.get("call_count"))
    rep = fu["repeated_observations"]
    rep_usage = rep.get("usage") if isinstance(rep.get("usage"), dict) else {}
    out["task_repeated_key_observations"] = {
        "calls": _int_or_zero(rep_usage.get("call_count")),
        "total_tokens": _int_or_zero(rep_usage.get("total_tokens")),
        "priced_calls": _int_or_zero(rep.get("priced_calls")),
        "unpriced_calls": _int_or_zero(rep.get("unpriced_calls")),
        "usd": _float_or_zero(rep.get("usd")),
        "conflicting": _int_or_zero(rep.get("conflicting")),
    }
    return out


def build_feature_usage_preview(task, task_id, feature_id, cycle_id, selectors, record_id=None,
                                basis="orchestrator-selected", reference=None):
    """Ready-to-save record-feature-usage payload for validated selectors,
    applying the same checks as recording (read-only). Returns
    (payload, already_linked_to_this_cycle)."""
    fu = _readable_feature_usage(task)
    _require_started_cycle(task, feature_id, cycle_id)
    call_keys = expand_feature_selectors(fu, selectors)
    digest = feature_call_set_sha256(call_keys)
    already = _check_assignment_conflicts(fu, call_keys, feature_id, cycle_id)
    if record_id is None:
        seed = "%s\n%s\n%s\n" % (feature_id, cycle_id, digest)
        record_id = "fu-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    if reference is None:
        reference = "feature-calls preview: %d call(s) via %d selector(s)" % (len(call_keys), len(selectors))
    payload = {
        "schema": FEATURE_USAGE_RECORD_SCHEMA,
        "schema_version": FEATURE_USAGE_RECORD_SCHEMA_VERSION,
        "record_id": _tracking_identifier(record_id, "record_id"),
        "task_id": task_id,
        "feature_id": feature_id,
        "cycle_id": cycle_id,
        "selectors": [dict(s) for s in selectors],
        "expected_call_count": len(call_keys),
        "expected_call_set_sha256": digest,
        "provenance": {
            "source": TRACKING_PROVENANCE_SOURCES[0],
            "basis": _tracking_choice(basis, "basis", FEATURE_USAGE_PROVENANCE_BASES),
            "reference": _tracking_reference(reference, "reference"),
        },
    }
    return payload, already


# ---- report ----------------------------------------------------------------

def _feature_tracking_present(task):
    """True when the task has delivery records or feature-usage links (or an
    unreadable block of either); otherwise the report is unchanged."""
    block = task.get("delivery")
    if block is not None and (not _delivery_shape_ok(block) or block["records"]):
        return True
    fu = task.get("feature_usage")
    if fu is None:
        return False
    if not _feature_usage_shape_ok(fu):
        return True
    return bool(fu["assignments"])


def _ledger_entry_cost(entry):
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
    status = pricing.get("status")
    return status, (_float_or_zero(pricing.get("usd")) if status == "priced" else 0.0)


def _blank_link_totals():
    return {"calls": 0, "priced_calls": 0, "unpriced_calls": 0, "usd": 0.0, "duration_ms": 0, "total_tokens": 0}


def _add_ledger_entry_to_totals(dst, entry):
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    status, usd = _ledger_entry_cost(entry)
    dst["calls"] += 1
    dst["total_tokens"] += _int_or_zero(usage.get("total_tokens"))
    dst["duration_ms"] += _int_or_zero(entry.get("duration_ms"))
    if status == "priced":
        dst["priced_calls"] += 1
        dst["usd"] += usd
    else:
        dst["unpriced_calls"] += 1


def summarize_feature_delivery(task):
    """Read-only summary for rendering. Linked cost sums ONLY the frozen
    per-call estimates of uniquely linked ledger calls; nothing is inferred."""
    out = {"delivery_status": "absent", "ledger_status": "absent", "cycles": {}, "features": {},
           "ledger": None, "conflicting_links": 0, "dangling_links": 0, "late_links": 0}
    block = task.get("delivery")
    cycles = {}
    if block is not None:
        if _delivery_shape_ok(block):
            out["delivery_status"] = "active"
            cycles = _delivery_cycles(block["records"])
        else:
            out["delivery_status"] = "unreadable"
    rows = out["cycles"]
    for cycle_id, c in cycles.items():
        started = c["started"]
        terminal = c["terminal"]
        start_epoch = _utc_ts_epoch(started.get("occurred_at")) if started else None
        end_epoch = _utc_ts_epoch(terminal.get("occurred_at")) if terminal else None
        if terminal is not None:
            status = terminal.get("event")
        elif started is not None:
            status = "in progress"
        else:
            status = "no start record"
        rows[cycle_id] = {
            "feature_id": c["feature_id"], "status": status, "feature_conflict": c["feature_conflict"],
            "started_at": started.get("occurred_at") if started else None,
            "ended_at": terminal.get("occurred_at") if terminal else None,
            "ended_recorded_at": terminal.get("recorded_at") if terminal else None,
            "elapsed_ms": (int(round((end_epoch - start_epoch) * 1000))
                           if start_epoch is not None and end_epoch is not None and end_epoch >= start_epoch
                           else None),
            "defects": len({r.get("finding_id") for r in c["defects"]}),
            "regressions": len({r.get("finding_id") for r in c["regressions"]}),
            "links": _blank_link_totals(),
        }
    fu = task.get("feature_usage")
    if fu is not None:
        out["ledger_status"] = "active" if _feature_usage_shape_ok(fu) else "unreadable"
    if out["ledger_status"] == "active":
        owners, conflicts = current_call_assignments(fu)
        out["conflicting_links"] = len(conflicts)
        ledger = {"since": fu.get("since"), "calls": _blank_link_totals(), "linked": _blank_link_totals(),
                  "unledgered": {}, "repeated": None, "baseline": fu["baseline"],
                  "invocations": len(fu["invocations"]),
                  "invocations_without_stable_ids": _int_or_zero(fu.get("invocations_without_stable_ids"))}
        for key, entry in fu["calls"].items():
            if not isinstance(entry, dict):
                continue
            _add_ledger_entry_to_totals(ledger["calls"], entry)
            owner = owners.get(key)
            if owner is None or key in conflicts:
                continue
            _add_ledger_entry_to_totals(ledger["linked"], entry)
            row = rows.get(owner[1])
            if row is None:
                row = rows[owner[1]] = {
                    "feature_id": owner[0], "status": "no readable delivery record", "feature_conflict": False,
                    "started_at": None, "ended_at": None, "ended_recorded_at": None, "elapsed_ms": None,
                    "defects": 0, "regressions": 0, "links": _blank_link_totals()}
            _add_ledger_entry_to_totals(row["links"], entry)
        out["dangling_links"] = sum(1 for k in owners if not isinstance(fu["calls"].get(k), dict))
        for rec in fu["assignments"].values():
            if not isinstance(rec, dict) or not isinstance(rec.get("cycle_id"), str):
                continue
            row = rows.get(rec.get("cycle_id"))
            ended = row.get("ended_recorded_at") if row else None
            if isinstance(ended, str) and isinstance(rec.get("recorded_at"), str) and rec["recorded_at"] > ended:
                out["late_links"] += 1
        for reason, bucket in fu["unledgered"].items():
            if isinstance(bucket, dict):
                ledger["unledgered"][reason] = bucket
        ledger["repeated"] = fu["repeated_observations"]
        out["ledger"] = ledger
    for cycle_id, row in rows.items():
        feat = out["features"].setdefault(row["feature_id"], {
            "completed": 0, "in_progress": 0, "cancelled": 0, "other": 0, "defects": 0, "regressions": 0,
            "links": _blank_link_totals()})
        key = {"completed": "completed", "cancelled": "cancelled", "in progress": "in_progress"}.get(
            row["status"], "other")
        feat[key] += 1
        feat["defects"] += row["defects"]
        feat["regressions"] += row["regressions"]
        for k in feat["links"]:
            feat["links"][k] += row["links"][k]
    return out


def _md_code(value):
    """Render an untrusted id/reference as an inline code span: no HTML,
    links or emphasis can render, and table cells cannot be split."""
    text = " ".join(("" if value is None else str(value)).split())
    for ch in ("`", "|", "\\"):
        text = text.replace(ch, "/")
    return "`%s`" % text if text else "—"


def _fmt_linked_cost(t):
    if not t["calls"]:
        return "no linked calls (unknown, not $0)"
    if not t["priced_calls"]:
        return "N/A — %d linked call(s), none priced (not $0)" % t["calls"]
    if t["unpriced_calls"]:
        return "$%.4f — **PARTIAL**: %d of %d linked calls priced, %d unpriced" % (
            t["usd"], t["priced_calls"], t["calls"], t["unpriced_calls"])
    return "$%.4f (all %d linked calls priced)" % (t["usd"], t["calls"])


def _fmt_findings(n, delivery_status):
    if delivery_status != "active":
        return "unknown"
    return "%d" % n if n else "none recorded"


def _fmt_bucket_usd(usd, priced, unpriced_or_legacy, calls):
    if not calls:
        return "—"
    if not priced:
        return "N/A — none priced (not $0)"
    if unpriced_or_legacy:
        return "$%.4f (partial)" % usd
    return "$%.4f" % usd


def render_feature_delivery_section(task):
    if not _feature_tracking_present(task):
        return []
    s = summarize_feature_delivery(task)
    lines = ["## Feature Delivery (explicit records and call links — not native telemetry)", ""]
    lines.append("_Lifecycle and confirmed findings come from `record-delivery`; usage comes only from exact "
                 "ledger call keys linked with `record-feature-usage` (each call counted once, in one cycle). "
                 "Nothing is assigned from timestamps, models, agent names, sessions or guesses._")
    lines.append("")
    if s["delivery_status"] == "unreadable":
        lines.append("> ⚠️ This task's delivery block has an unsupported or malformed schema; it is left "
                     "unchanged and not interpreted.")
        lines.append("")
    if s["ledger_status"] == "unreadable":
        lines.append("> ⚠️ This task's feature-usage ledger has an unsupported or malformed schema; linked usage "
                     "is unknown.")
        lines.append("")
    if s["cycles"]:
        lines.append("### Delivery cycles")
        lines.append("")
        lines.append("| Feature | Cycle | Status | Started (UTC) | Ended (UTC) | Elapsed (wall clock, incl. idle) "
                     "| Linked calls | Est. USD of linked calls (frozen at ingestion) | Linked model-call time "
                     "| Confirmed defects | Confirmed regressions |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for cycle_id, row in sorted(s["cycles"].items(), key=lambda kv: (str(kv[1]["feature_id"]),
                                                                          str(kv[1]["started_at"] or ""), kv[0])):
            links = row["links"]
            elapsed = fmt_duration_ms(row["elapsed_ms"]) if row["elapsed_ms"] is not None else (
                "unknown (in progress)" if row["status"] == "in progress" else "unknown")
            status = row["status"] + (" ⚠️ cycle recorded under several features" if row["feature_conflict"] else "")
            lines.append("| %s | %s | %s | %s | %s | %s | %d | %s | %s | %s | %s |" % (
                _md_code(row["feature_id"]), _md_code(cycle_id), _md_cell(status), _md_code(row["started_at"]),
                _md_code(row["ended_at"]), elapsed, links["calls"], _fmt_linked_cost(links),
                fmt_duration_ms(links["duration_ms"]) if links["calls"] else "—",
                _fmt_findings(row["defects"], s["delivery_status"]),
                _fmt_findings(row["regressions"], s["delivery_status"])))
        lines.append("")
        lines.append("### Per feature")
        lines.append("")
        lines.append("| Feature | Cycles completed / in progress / cancelled | Linked calls | Est. USD of linked "
                     "calls | Linked model-call time | Confirmed defects | Confirmed regressions |")
        lines.append("|---|---|---|---|---|---|---|")
        for feature_id, f in sorted(s["features"].items(), key=lambda kv: str(kv[0])):
            cycles_cell = "%d / %d / %d" % (f["completed"], f["in_progress"], f["cancelled"])
            if f["other"]:
                cycles_cell += " (+%d without a readable lifecycle)" % f["other"]
            lines.append("| %s | %s | %d | %s | %s | %s | %s |" % (
                _md_code(feature_id), cycles_cell, f["links"]["calls"], _fmt_linked_cost(f["links"]),
                fmt_duration_ms(f["links"]["duration_ms"]) if f["links"]["calls"] else "—",
                _fmt_findings(f["defects"], s["delivery_status"]),
                _fmt_findings(f["regressions"], s["delivery_status"])))
        lines.append("")
    notes = [
        "Linked cost is a subset of the task's Summary estimate, never added to it again: it sums the per-call "
        "official estimates frozen when each call was ingested (linking later never reprices). Unpriced linked "
        "calls make it a lower bound; a feature without linked calls has unknown cost, not $0.",
        "Elapsed is wall-clock time from the recorded start to the recorded completion/cancellation (includes "
        "idle and user time). Linked model-call time is the separate sum of the linked calls' span durations.",
        "Defects/regressions count only explicitly confirmed material findings (critical/high/medium "
        "correctness, security, safety or data-integrity), each finding id once; a regression requires recorded "
        "evidence of previously working behavior. \"none recorded\" is not evidence of no defects — coverage is "
        "limited to what was recorded.",
    ]
    if s["late_links"]:
        notes.append("%d link record(s) were added after their cycle ended (explicit late audit links; the "
                     "lifecycle is unchanged and their prices are the frozen ingestion estimates)." % s["late_links"])
    if s["conflicting_links"]:
        notes.append("⚠️ %d call(s) have conflicting stored links (hand-edited data?) and are excluded from every "
                     "feature." % s["conflicting_links"])
    if s["dangling_links"]:
        notes.append("⚠️ %d linked call key(s) are missing from the ledger and are not counted." % s["dangling_links"])
    for n in notes:
        lines.append("- " + n)
    lines.append("")

    ledger = s["ledger"]
    if ledger is None:
        return lines
    lines.append("### Feature usage ledger coverage")
    lines.append("")
    lines.append("_Exclusive buckets that must add up to the Summary totals. Usage recorded before the ledger "
                 "started (%s) is a historical aggregate that can never be split by feature._"
                 % _md_cell(ledger["since"] or "unknown"))
    lines.append("")
    lines.append("| Bucket | Calls | Total tokens | Priced calls | Unpriced or legacy calls | Est. USD (priced "
                 "subtotal) |")
    lines.append("|---|---|---|---|---|---|")
    base_usage = ledger["baseline"].get("usage") if isinstance(ledger["baseline"].get("usage"), dict) else {}
    base_cost = (ledger["baseline"].get("official_cost")
                 if isinstance(ledger["baseline"].get("official_cost"), dict) else {})
    base_calls = _int_or_zero(base_usage.get("call_count"))
    base_priced = _int_or_zero(base_cost.get("priced_calls"))
    base_other = _int_or_zero(base_cost.get("unpriced_calls")) + _int_or_zero(base_cost.get("legacy_calls"))
    base_usd = _float_or_zero(base_cost.get("usd"))
    sum_calls = base_calls
    sum_tokens = _int_or_zero(base_usage.get("total_tokens"))
    sum_usd = base_usd
    lines.append("| Pre-ledger baseline (historical, unsplittable) | %d | %d | %d | %d | %s |" % (
        base_calls, sum_tokens, base_priced, base_other, _fmt_bucket_usd(base_usd, base_priced, base_other,
                                                                         base_calls)))
    linked, allc = ledger["linked"], ledger["calls"]
    unlinked = {k: allc[k] - linked[k] for k in allc}
    for label, t in (("Ledger — linked to a feature cycle", linked),
                     ("Ledger — not linked to any feature (unallocated)", unlinked)):
        lines.append("| %s | %d | %d | %d | %d | %s |" % (
            label, t["calls"], t["total_tokens"], t["priced_calls"], t["unpriced_calls"],
            _fmt_bucket_usd(t["usd"], t["priced_calls"], t["unpriced_calls"], t["calls"])))
    sum_calls += allc["calls"]
    sum_tokens += allc["total_tokens"]
    sum_usd += allc["usd"]
    extra_rows = [("No stable call key — %s" % reason, bucket) for reason, bucket in sorted(ledger["unledgered"].items())]
    if ledger["repeated"]:
        extra_rows.append(("Repeated stable key (kept in totals by the existing aggregation; never ledgered or "
                           "linked twice)", ledger["repeated"]))
    for label, bucket in extra_rows:
        usage = bucket.get("usage") if isinstance(bucket.get("usage"), dict) else {}
        calls = _int_or_zero(usage.get("call_count"))
        if not calls:
            continue
        priced = _int_or_zero(bucket.get("priced_calls"))
        other = calls - priced
        usd = _float_or_zero(bucket.get("usd"))
        lines.append("| %s | %d | %d | %d | %d | %s |" % (
            _md_cell(label), calls, _int_or_zero(usage.get("total_tokens")), priced, other,
            _fmt_bucket_usd(usd, priced, other, calls)))
        sum_calls += calls
        sum_tokens += _int_or_zero(usage.get("total_tokens"))
        sum_usd += usd
    totals = task.get("totals") if isinstance(task.get("totals"), dict) else {}
    official = summarize_official_cost(task)["totals"]
    total_calls = _int_or_zero(totals.get("call_count"))
    total_tokens = _int_or_zero(totals.get("total_tokens"))
    lines.append("| **Total (Summary)** | %d | %d | %d | %d | %s |" % (
        total_calls, total_tokens, official["priced_calls"],
        official["unpriced_calls"] + official["legacy_calls"],
        _fmt_bucket_usd(official["usd"], official["priced_calls"],
                        official["unpriced_calls"] + official["legacy_calls"], official["calls"])))
    lines.append("")
    reconciled = (sum_calls == total_calls and sum_tokens == total_tokens
                  and math.isclose(sum_usd, official["usd"], rel_tol=1e-9, abs_tol=1e-6))
    if not reconciled:
        lines.append("> ⚠️ Ledger buckets do not reconcile with the Summary totals/estimate; treat feature "
                     "usage for this task as incomplete.")
        lines.append("")
    repeated = ledger["repeated"] or {}
    diagnostics = []
    rep_usage = repeated.get("usage") if isinstance(repeated.get("usage"), dict) else {}
    if _int_or_zero(rep_usage.get("call_count")):
        rep_priced = _int_or_zero(repeated.get("priced_calls"))
        rep_calls = _int_or_zero(rep_usage.get("call_count"))
        diagnostics.append(
            "%d call observation(s) (%d tokens, %d priced at %s, %d unpriced/not recorded) repeated a call key "
            "already in the ledger. The existing aggregation keeps them in the Summary totals and estimate, so "
            "those totals are not a count of unique calls; the ledger and every feature count each key once."
            % (rep_calls, _int_or_zero(rep_usage.get("total_tokens")), rep_priced,
               "$%.4f" % _float_or_zero(repeated.get("usd")) if rep_priced else "N/A",
               rep_calls - rep_priced))
    if _int_or_zero(repeated.get("conflicting")):
        diagnostics.append("%d repeated observation(s) of an already-ledgered call key carried different data; "
                           "the first observation is kept unchanged." % _int_or_zero(repeated.get("conflicting")))
    if ledger["invocations_without_stable_ids"]:
        diagnostics.append("%d invoke_agent span(s) had no valid ids and cannot be selected as invocations."
                           % ledger["invocations_without_stable_ids"])
    for d in diagnostics:
        lines.append("- " + d)
    if diagnostics:
        lines.append("")
    return lines


# ---- commands ----------------------------------------------------------------

def _record_tracking(record, apply_fn):
    """Shared locked read-validate-write for record-delivery and
    record-feature-usage. Both require a task file that ingest already
    created: they never create a task (no phantom usage/report state).
    Returns (exit_code, outcome)."""
    task_id = record["task_id"]
    with ingest_lock():
        tpath = task_path(task_id)
        task, error = _load_task_file_strict(tpath, task_id)
        if error:
            print("Error: %s" % error, file=sys.stderr)
            return 1, None
        if task is None:
            print("Error: no recorded usage for task '%s' yet; ingest a session of this task first (delivery and "
                  "feature-usage records are only added to an existing task)" % task_id, file=sys.stderr)
            return 1, None
        try:
            outcome = apply_fn(task, record)
        except TrackingRecordError as ex:
            print("Error: record rejected: %s" % ex, file=sys.stderr)
            return 1, None
        if outcome != "duplicate":
            atomic_write(tpath, json.dumps(task, indent=2))
    return 0, outcome


def cmd_record_delivery(args):
    """Record one validated delivery lifecycle/finding event. Never ingests,
    fetches pricing or regenerates the Markdown report."""
    try:
        record = load_delivery_record_input(args.input)
    except TrackingRecordError as ex:
        print("Error: invalid delivery record: %s" % ex, file=sys.stderr)
        return 1
    code, outcome = _record_tracking(record, apply_delivery_record)
    if code:
        return code
    if outcome == "duplicate":
        print("Delivery record '%s' is already recorded identically for task '%s'; nothing changed."
              % (record["record_id"], record["task_id"]))
        return 0
    print("Recorded delivery event '%s' (%s; task '%s', feature '%s', cycle '%s'). The Markdown report is not "
          "regenerated by this command; run `copilot-task-report.py report %s` to refresh it."
          % (record["record_id"], record["event"], record["task_id"], record["feature_id"], record["cycle_id"],
             record["task_id"]))
    return 0


def cmd_record_feature_usage(args):
    """Record one validated explicit feature-usage link. Never ingests,
    fetches pricing or regenerates the Markdown report."""
    try:
        record = load_feature_usage_record_input(args.input)
    except TrackingRecordError as ex:
        print("Error: invalid feature-usage record: %s" % ex, file=sys.stderr)
        return 1
    code, outcome = _record_tracking(record, apply_feature_usage_record)
    if code:
        return code
    if outcome == "duplicate":
        print("Feature-usage record '%s' is already recorded identically for task '%s'; nothing changed."
              % (record["record_id"], record["task_id"]))
        return 0
    print("Recorded feature-usage link '%s' (%d call(s); task '%s', feature '%s', cycle '%s'). The Markdown report "
          "is not regenerated by this command; run `copilot-task-report.py report %s` to refresh it."
          % (record["record_id"], record["expected_call_count"], record["task_id"], record["feature_id"],
             record["cycle_id"], record["task_id"]))
    return 0


def _parse_cli_selector(value, kind, session_id):
    parts = value.split("/") if isinstance(value, str) else []
    if len(parts) != 2:
        raise TrackingRecordError("--%s must be TRACE_ID/SPAN_ID (got %r)" % (
            "call" if kind == "call" else "invocation", value))
    return {"kind": kind, "session_id": session_id, "trace_id": parts[0], "span_id": parts[1]}


def cmd_feature_calls(args):
    """Read-only: list a session's ledger calls/invocations, or (with
    selectors + --feature-id/--cycle-id) print a ready-to-save
    record-feature-usage payload. Takes no lock, creates no files or
    directories, and never ingests, fetches pricing or uses the network."""
    task_id = normalize_task_id(args.task)
    if task_id is None:
        print("Error: '%s' is not a valid task ID/name." % args.task, file=sys.stderr)
        return 1
    session_id = args.session_id
    if not _valid_ledger_session_id(session_id):
        print("Error: '%s' is not a valid session id." % session_id, file=sys.stderr)
        return 1
    task, error = _load_task_file_strict(task_path(task_id), task_id)
    if error:
        print("Error: %s" % error, file=sys.stderr)
        return 1
    if task is None:
        print("No recorded usage for task '%s' yet." % task_id, file=sys.stderr)
        return 1
    raw_selectors = ([("direct-invocation", v) for v in (args.invocation or [])]
                     + [("call", v) for v in (args.call or [])])
    preview_options = (args.feature_id, args.cycle_id, args.record_id, args.basis, args.reference)
    if not raw_selectors and all(v is None for v in preview_options):
        print(json.dumps(feature_calls_listing(task, task_id, session_id), indent=2))
        return 0
    try:
        if not raw_selectors or args.feature_id is None or args.cycle_id is None:
            raise TrackingRecordError("a preview needs --feature-id, --cycle-id and at least one --invocation or "
                                      "--call selector")
        feature_id = _tracking_identifier(args.feature_id, "--feature-id")
        cycle_id = _tracking_identifier(args.cycle_id, "--cycle-id")
        selectors = _validate_selectors([_parse_cli_selector(v, k, session_id) for k, v in raw_selectors])
        payload, already = build_feature_usage_preview(
            task, task_id, feature_id, cycle_id, selectors, record_id=args.record_id,
            basis=args.basis or "orchestrator-selected", reference=args.reference)
    except TrackingRecordError as ex:
        print("Error: preview rejected: %s" % ex, file=sys.stderr)
        return 1
    if already:
        print("Note: %d of the %d selected call(s) are already linked to this cycle; they will be counted once."
              % (already, payload["expected_call_count"]), file=sys.stderr)
    print(json.dumps(payload, indent=2))
    return 0


# --------------------------------------------------------------------------
# Per-session context metrics sidecar (copilot-s new sessions only)
# --------------------------------------------------------------------------
#
# Sessions created by copilot-s (after this feature exists) and identified
# unambiguously after the run are "enrolled": a small sidecar JSON file is
# written into the session's own state directory. Nothing else is ever
# enrolled or backfilled — legacy, imported or externally created sessions
# never get a sidecar, so copilot-s never scans their event logs.
#
# What is recorded (and nothing more — no message text, no summaryContent):
#   - last_precompaction_snapshot: currentTokens / tokenLimit from the LATEST
#     valid `session.compaction_start` event. This is the context fill
#     *last observed right before a compaction* — NOT the live/current or
#     at-exit context usage. `session.shutdown` carries currentTokens but no
#     verified tokenLimit (and the model may have changed since the last
#     compaction), so it is deliberately never paired with an old limit.
#     Values above 100% are kept as recorded, never capped. Token fields
#     must be strict ints within [0, 2**53] / [1, 2**53]; anything else is
#     counted in invalid_snapshot_events and never stored.
#   - successful_compactions: number of `session.compaction_complete` events
#     whose `data.success` is exactly true (starts and failed completions are
#     not counted).
#   - an incremental read cursor over events.jsonl (byte offset of the last
#     complete line + a fingerprint of the head of the file, so a truncated or
#     rewritten log triggers a full rescan instead of a silent miscount), plus
#     malformed-line / partial-trailing-line bookkeeping so an unreadable log
#     is reported as such and never turned into zeros.
#
# These commands never touch task reports, pricing, ingest state or legacy
# migrations (they are dispatched before run_startup_migrations()).

CONTEXT_SIDECAR_NAME = "copilot-s-context.json"
CONTEXT_SCHEMA = "copilot-s-context"
CONTEXT_SCHEMA_VERSION = 1
CONTEXT_HEAD_FINGERPRINT_BYTES = 4096
CONTEXT_TRIGGER_MAX_LEN = 40
# Tolerance (seconds) when checking that a newly-detected session was created
# after copilot-s launched copilot (clock granularity / filesystem skew).
CONTEXT_ENROLL_SKEW_SECONDS = 5
# Upper bound for currentTokens / tokenLimit and the integer counters in the
# sidecar: larger values are rejected (never stored) so formatting/percent
# math can never overflow a float or produce a nonsense percentage.
CONTEXT_MAX_INT = 2 ** 53
_CONTEXT_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ContextSidecarError(ValueError):
    pass


def _context_valid_session_id(session_id):
    return bool(session_id) and bool(_CONTEXT_SESSION_ID_RE.match(session_id)) and session_id not in (".", "..")


def _is_strict_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _context_int_in_range(value, minimum):
    """Strict (non-bool) int within [minimum, CONTEXT_MAX_INT]."""
    return _is_strict_int(value) and minimum <= value <= CONTEXT_MAX_INT


def context_sidecar_path(session_dir):
    return os.path.join(session_dir, CONTEXT_SIDECAR_NAME)


def _context_blank_events_state():
    return {
        "offset": 0,
        "head_len": 0,
        "head_sha256": "",
        "malformed_lines": 0,
        "invalid_snapshot_events": 0,
        "trailing_partial_line": False,
        "status": "not_read",
        "last_error": None,
        "last_read_ok_at": None,
    }


def _context_new_sidecar(session_id):
    ts = now_iso()
    return {
        "schema": CONTEXT_SCHEMA,
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "session_id": session_id,
        "enrolled_by": "copilot-s",
        "enrolled_at": ts,
        "updated_at": ts,
        "successful_compactions": 0,
        "last_precompaction_snapshot": None,
        "events": _context_blank_events_state(),
    }


def _validate_context_snapshot(snap):
    if snap is None:
        return
    if not isinstance(snap, dict):
        raise ContextSidecarError("last_precompaction_snapshot is not an object")
    cur, lim = snap.get("current_tokens"), snap.get("token_limit")
    if not _context_int_in_range(cur, 0):
        raise ContextSidecarError("snapshot current_tokens invalid")
    if not _context_int_in_range(lim, 1):
        raise ContextSidecarError("snapshot token_limit invalid")
    for key in ("trigger", "timestamp"):
        if snap.get(key) is not None and not isinstance(snap.get(key), str):
            raise ContextSidecarError("snapshot %s invalid" % key)


def validate_context_sidecar(data, session_id):
    """Strictly validate a parsed sidecar for `session_id`. Raises
    ContextSidecarError on anything unexpected (never guesses/repairs)."""
    if not isinstance(data, dict):
        raise ContextSidecarError("not a JSON object")
    if data.get("schema") != CONTEXT_SCHEMA:
        raise ContextSidecarError("unknown schema")
    if data.get("schema_version") != CONTEXT_SCHEMA_VERSION:
        raise ContextSidecarError("unsupported schema_version %r" % (data.get("schema_version"),))
    if data.get("session_id") != session_id:
        raise ContextSidecarError("session_id does not match its session directory")
    count = data.get("successful_compactions")
    if not _context_int_in_range(count, 0):
        raise ContextSidecarError("successful_compactions invalid")
    _validate_context_snapshot(data.get("last_precompaction_snapshot"))
    ev = data.get("events")
    if not isinstance(ev, dict):
        raise ContextSidecarError("events cursor missing")
    for key in ("offset", "head_len", "malformed_lines", "invalid_snapshot_events"):
        if not _context_int_in_range(ev.get(key), 0):
            raise ContextSidecarError("events.%s invalid" % key)
    if ev.get("head_len") > ev.get("offset") or ev.get("head_len") > CONTEXT_HEAD_FINGERPRINT_BYTES:
        raise ContextSidecarError("events.head_len inconsistent")
    if not isinstance(ev.get("head_sha256"), str):
        raise ContextSidecarError("events.head_sha256 invalid")
    if not isinstance(ev.get("trailing_partial_line"), bool):
        raise ContextSidecarError("events.trailing_partial_line invalid")
    if ev.get("status") not in ("not_read", "complete", "partial", "missing", "unreadable"):
        raise ContextSidecarError("events.status invalid")
    return data


def load_context_sidecar(session_dir, session_id):
    """Return (data, error). data is None when the sidecar does not exist
    (error None) or is unreadable/invalid (error set)."""
    path = context_sidecar_path(session_dir)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "rb") as f:
            raw = f.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ContextSidecarError("file too large")
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        return validate_context_sidecar(data, session_id), None
    except (OSError, ValueError, UnicodeDecodeError, TypeError, OverflowError, RecursionError,
            ContextSidecarError) as ex:
        return None, str(ex) or ex.__class__.__name__


def _context_apply_event(obj, sidecar):
    """Fold one parsed event into the sidecar. Only the two compaction event
    types are consulted, and only the numeric/trigger fields are copied."""
    if not isinstance(obj, dict):
        return
    etype = obj.get("type")
    if etype not in ("session.compaction_start", "session.compaction_complete"):
        return
    data = obj.get("data")
    if not isinstance(data, dict):
        data = {}
    if etype == "session.compaction_complete":
        if data.get("success") is True:
            sidecar["successful_compactions"] += 1
        return
    cur, lim = data.get("currentTokens"), data.get("tokenLimit")
    # Out-of-range/bool/float/missing values are counted as invalid and never
    # replace the previous valid snapshot (bounded so no huge int is stored).
    if not (_context_int_in_range(cur, 0) and _context_int_in_range(lim, 1)):
        sidecar["events"]["invalid_snapshot_events"] += 1
        return
    trigger = data.get("trigger")
    if isinstance(trigger, str):
        trigger = re.sub(r"[^A-Za-z0-9_.:-]", "", trigger)[:CONTEXT_TRIGGER_MAX_LEN] or None
    else:
        trigger = None
    ts = obj.get("timestamp")
    if not (isinstance(ts, str) and parse_ts(ts) is not None and len(ts) <= 64):
        ts = None
    sidecar["last_precompaction_snapshot"] = {
        "current_tokens": cur,
        "token_limit": lim,
        "trigger": trigger,
        "timestamp": ts,
    }


def _context_reset_scan(sidecar):
    sidecar["successful_compactions"] = 0
    sidecar["last_precompaction_snapshot"] = None
    sidecar["events"] = _context_blank_events_state()


def context_scan_events(sidecar, events_path):
    """Incrementally fold events.jsonl into `sidecar` (mutated in place).

    Resumes from the stored byte offset when the file still has the same head
    and is at least that long; otherwise (truncated/rewritten/replaced) it
    resets and rescans from byte 0. A trailing line without a newline is
    never consumed (it is re-read next time) and is flagged as partial. A
    missing/unreadable log leaves the cached metrics untouched and records
    the status, so it is never reported as zero compactions.

    All counting happens on a private copy that is committed only after the
    read finished, so an I/O error mid-read can never leave counts advanced
    without the matching cursor (which would double count next time)."""
    ev_orig = sidecar["events"]
    try:
        f = open(events_path, "rb")
    except FileNotFoundError:
        ev_orig["status"] = "missing"
        ev_orig["last_error"] = "events.jsonl not found"
        return
    except OSError as ex:
        ev_orig["status"] = "unreadable"
        ev_orig["last_error"] = ex.__class__.__name__
        return
    work = json.loads(json.dumps(sidecar))
    try:
        with f:
            size = os.fstat(f.fileno()).st_size
            offset = work["events"]["offset"]
            rescan = size < offset
            if not rescan and work["events"]["head_len"] > 0:
                head = f.read(work["events"]["head_len"])
                if hashlib.sha256(head).hexdigest() != work["events"]["head_sha256"]:
                    rescan = True
            if rescan:
                _context_reset_scan(work)
                offset = 0
            ev = work["events"]
            f.seek(offset)
            trailing_partial = False
            for raw in f:
                if not raw.endswith(b"\n"):
                    trailing_partial = True
                    break
                offset += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
                except (ValueError, UnicodeDecodeError, TypeError, OverflowError, RecursionError):
                    ev["malformed_lines"] += 1
                    continue
                _context_apply_event(obj, work)
            head_len = min(offset, CONTEXT_HEAD_FINGERPRINT_BYTES)
            f.seek(0)
            head = f.read(head_len)
    except OSError as ex:
        ev_orig["status"] = "unreadable"
        ev_orig["last_error"] = ex.__class__.__name__
        return
    sidecar.clear()
    sidecar.update(work)
    ev = sidecar["events"]
    ev["offset"] = offset
    ev["head_len"] = len(head)
    ev["head_sha256"] = hashlib.sha256(head).hexdigest()
    ev["trailing_partial_line"] = trailing_partial
    ev["status"] = "partial" if (trailing_partial or ev["malformed_lines"]) else "complete"
    ev["last_error"] = None
    ev["last_read_ok_at"] = now_iso()


def _fmt_context_percent(snap):
    pct = snap["current_tokens"] * 100.0 / snap["token_limit"]
    return "%.1f%%" % pct


def format_context_metrics(sidecar, compact=False):
    """Human-readable, ANSI-free one-line summary of a validated sidecar."""
    ev = sidecar["events"]
    snap = sidecar.get("last_precompaction_snapshot")
    status = ev.get("status")
    count = sidecar["successful_compactions"]
    unknown_count = status in ("missing", "unreadable", "not_read")
    lower_bound = status == "partial"
    if compact:
        ctx = ("ctx %s last observed before compaction" % _fmt_context_percent(snap)) if snap else "ctx N/A"
        if unknown_count:
            cmp_txt = "compactions unknown"
        else:
            cmp_txt = "%s%d compaction%s" % (">=" if lower_bound else "", count, "" if count == 1 else "s")
        return "%s · %s%s" % (ctx, cmp_txt, " (log incomplete)" if (lower_bound or unknown_count) else "")
    if snap:
        ctx = "Context: %s (%s/%s tokens) last observed before compaction" % (
            _fmt_context_percent(snap), format(snap["current_tokens"], ","), format(snap["token_limit"], ","))
    else:
        ctx = "Context: N/A (no valid pre-compaction snapshot recorded)"
    if unknown_count:
        cmp_txt = "Compactions: unknown (events log %s)" % ("missing" if status == "missing" else "unreadable")
    else:
        cmp_txt = "Compactions: %s%d successful" % (">=" if lower_bound else "", count)
    parts = [ctx, cmp_txt]
    if lower_bound:
        issues = []
        if ev.get("malformed_lines"):
            issues.append("%d unreadable line(s)" % ev["malformed_lines"])
        if ev.get("trailing_partial_line"):
            issues.append("truncated last line")
        parts.append("events log incomplete: %s" % ", ".join(issues))
    if ev.get("invalid_snapshot_events"):
        parts.append("%d compaction start(s) without valid token fields ignored" % ev["invalid_snapshot_events"])
    return " · ".join(parts)


def _read_workspace_field(session_dir, field):
    """Read a top-level `field: value` line from workspace.yaml (the same
    simple line format copilot-s reads with grep/sed)."""
    path = os.path.join(session_dir, "workspace.yaml")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return line[len(field) + 1:].strip()
    except OSError:
        return None
    return None


def _context_created_epoch(session_dir):
    """workspace.yaml created_at as a finite epoch, or None when it is
    missing, empty or unparseable."""
    created_raw = (_read_workspace_field(session_dir, "created_at") or "").strip().strip("'\"")
    if not created_raw:
        return None
    try:
        created = parse_ts(created_raw)
    except (ValueError, OverflowError, OSError):
        return None
    if created is None or not math.isfinite(created):
        return None
    return created


def cmd_context_update(args):
    """Enroll (with --enroll) or refresh an already-enrolled session's
    context-metrics sidecar, then print a one-line summary. Without
    --enroll, a session that has no sidecar is left completely untouched."""
    session_id = args.session_id
    if not _context_valid_session_id(session_id):
        print("Error: invalid session id for context metrics.", file=sys.stderr)
        return 1
    if args.started_after is not None and not math.isfinite(args.started_after):
        print("Error: --started-after must be a finite epoch; context metrics not recorded.", file=sys.stderr)
        return 1
    session_dir = args.session_dir or os.path.join(SESSION_STATE_DIR, session_id)
    if os.path.basename(os.path.normpath(session_dir)) != session_id or not os.path.isdir(session_dir):
        print("Error: session directory does not match session id; context metrics not recorded.", file=sys.stderr)
        return 1

    sidecar, error = load_context_sidecar(session_dir, session_id)
    if sidecar is None and error is None:
        if not args.enroll:
            return 0  # not enrolled: never create, never scan
        if args.expect_cwd is not None:
            cwd = _read_workspace_field(session_dir, "cwd")
            if cwd != args.expect_cwd:
                print("Warning: new session's recorded cwd does not match the launch directory; "
                      "context metrics not recorded for this session.", file=sys.stderr)
                return 3
        if args.started_after is not None:
            # When a launch time is supplied, a session whose creation time
            # cannot be verified is never enrolled (it might be an old one).
            created = _context_created_epoch(session_dir)
            if created is None:
                print("Warning: detected session has no readable created_at in workspace.yaml, so it "
                      "cannot be verified as created by this copilot launch; context metrics not "
                      "recorded for this session.", file=sys.stderr)
                return 3
            if created < args.started_after - CONTEXT_ENROLL_SKEW_SECONDS:
                print("Warning: detected session predates this copilot launch; context metrics not "
                      "recorded for this session.", file=sys.stderr)
                return 3
        sidecar = _context_new_sidecar(session_id)
    elif sidecar is None:
        # The sidecar name is only ever written by this command, so its
        # presence means the session was enrolled; rebuild it from a full
        # rescan rather than trusting or silently discarding bad content.
        print("Warning: context metrics file for this session was unreadable (%s); rebuilding it "
              "from a full rescan of the events log." % error, file=sys.stderr)
        sidecar = _context_new_sidecar(session_id)

    context_scan_events(sidecar, os.path.join(session_dir, "events.jsonl"))
    sidecar["updated_at"] = now_iso()
    try:
        validate_context_sidecar(sidecar, session_id)
    except ContextSidecarError as ex:
        # Never persist state that a later read would reject.
        print("Error: computed context metrics are invalid (%s); sidecar not written." % ex, file=sys.stderr)
        return 1
    atomic_write(context_sidecar_path(session_dir), json.dumps(sidecar, indent=2) + "\n")
    if sidecar["events"]["status"] in ("missing", "unreadable"):
        print("Warning: could not read this session's events log (%s); compaction metrics are "
              "unknown, not zero." % sidecar["events"]["last_error"], file=sys.stderr)
    print(format_context_metrics(sidecar))
    return 0


def cmd_context_list(args):
    """Read session ids (one per line) from stdin and print exactly one line
    per input line: a compact metrics label from that session's existing
    sidecar, or an empty line. Reads sidecars only — never events logs —
    and never creates or modifies anything."""
    state_dir = args.session_state_dir or SESSION_STATE_DIR
    for raw in sys.stdin:
        session_id = raw.rstrip("\r\n")
        label = ""
        if _context_valid_session_id(session_id):
            sdir = os.path.join(state_dir, session_id)
            sidecar, error = load_context_sidecar(sdir, session_id)
            if sidecar is not None:
                # One bad sidecar must only affect its own line, never abort
                # the labels of the other sessions.
                try:
                    label = format_context_metrics(sidecar, compact=True)
                except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
                    label = "ctx metrics unreadable"
            elif error is not None:
                label = "ctx metrics unreadable"
        print(label)
    return 0


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

    # Refresh the official pricing cache (at most once per command, only if
    # due, bounded) BEFORE taking the ingest lock, so a slow network never
    # holds up other copilot-s ingests. The snapshot read right after is the
    # one that prices this command's calls.
    refresh_official_pricing_if_due()
    pricing_ctx = load_official_pricing_context()

    with ingest_lock():
        state = load_json(STATE_FILE, {})
        sess_state = state.setdefault(session_id, {})

        # --task may be blank (reuse last known) or invalid (safely fall
        # back rather than silently accepting garbage as a task ID).
        requested = normalize_task_id(args.task) if args.task else None
        if args.task and requested is None:
            print("Warning: ignoring invalid task ID '%s' from caller; falling back to "
                  "last-known/UNASSIGNED." % args.task, file=sys.stderr)
        task_id = requested or sess_state.get("task_id") or UNASSIGNED
        sess_state["task_id"] = task_id

        # Validate the destination task file BEFORE reading any telemetry or
        # writing session state: task files now hold irreplaceable review
        # records, so an unreadable/foreign file must never be replaced by
        # an empty task. Refusing here leaves the task file, the state file
        # and every offset/cursor untouched, so nothing is consumed and a
        # later ingest (after the file is repaired) picks the data up.
        existing_task, task_error = _load_task_file_strict(task_path(task_id), task_id)
        if task_error:
            print("Error: %s. Session '%s' was not ingested and its offsets were not advanced; "
                  "repair or move the file, then ingest again." % (task_error, session_id),
                  file=sys.stderr)
            return 1

        # events.jsonl and OTEL are independent data sources with
        # independent lifecycles: build_delta seeds/reads each of them on
        # its own, so a not-yet-existing events.jsonl never suppresses OTEL
        # ingestion (and vice versa) — see build_delta's docstring/comments.
        delta = build_delta(session_id, sess_state, events_path, pricing_ctx=pricing_ctx)

        if (
            delta["model_calls"] == 0
            and delta["nano_aiu_delta"] == 0
            and delta["premium_requests_delta"] == 0
            and not delta["agent_summaries"]
            and not delta["repositories"]
            and not delta["attribution"]["invocation_events"]
            and not delta["attribution"]["attributed"]
        ):
            # Still persist offsets/cursors so we don't rescan the same
            # bytes forever, but nothing to merge into the task. Every
            # mergeable delta field must be checked here — a premium-only,
            # repository-only or invocation-registry-only update (e.g. a
            # bare subagent.started with no model calls yet, or an
            # invoke_agent span whose child calls were read earlier) would
            # otherwise be silently dropped even though build_delta's
            # advanced offsets mean it can never be re-derived on a later
            # ingest.
            atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))
            return 0

        # Crash-safety ordering: persist the advanced offsets/cursors FIRST.
        # If we crash before the task write below, the worst case is an
        # undercounted task (safe to re-run later without double
        # counting) rather than a double-counted one.
        atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))

        # Validated above under this same lock; a missing file is a new task.
        task = existing_task if existing_task is not None else {}
        task = merge_delta_into_task(task, delta, session_id, task_id)
        atomic_write(task_path(task_id), json.dumps(task, indent=2))

        md = render_markdown(task)
        atomic_write(report_path(task_id), md)
    return 0


def cmd_report(args):
    ensure_dirs()
    task_id = normalize_task_id(args.task_id)
    if task_id is None:
        # Never fall back to a raw/uppercased-only value here: an invalid
        # key must fail clearly rather than silently mapping to whatever
        # garbage the caller passed (which could read/write the wrong
        # task file, or one that looks plausible but was never ingested
        # under that exact form).
        print(
            "Error: '%s' is not a valid task ID/name (expected e.g. ABC-123, a free-form "
            "name using letters/digits/./_/- up to %d characters, or the literal '%s')."
            % (args.task_id, MAX_TASK_ID_LEN, UNASSIGNED),
            file=sys.stderr,
        )
        return 1
    tpath = task_path(task_id)
    task = load_json(tpath, None)
    if task is None:
        print("No recorded usage for task '%s' yet." % task_id, file=sys.stderr)
        return 1
    # Keeps the cache status shown in the report current. Recorded cost
    # estimates are never recomputed from the refreshed rates.
    refresh_official_pricing_if_due()
    md = render_markdown(task)
    with ingest_lock():
        atomic_write(report_path(task_id), md)
    print(md)
    print("\n(report file: %s)" % report_path(task_id), file=sys.stderr)
    return 0


def cmd_normalize(args):
    """Validate/normalize a raw task ID or free-form task name and print
    the normalized form on success. Used by copilot-s to keep the
    "what's a valid task ID" logic in exactly one place (this module)
    rather than duplicating the sanitization rules in Bash."""
    result = normalize_task_id(args.raw)
    if result is None:
        print(
            "Error: '%s' is not a valid task ID/name (expected e.g. ABC-123, a free-form "
            "name using letters/digits/./_/- up to %d characters, or the literal '%s')."
            % (args.raw, MAX_TASK_ID_LEN, UNASSIGNED),
            file=sys.stderr,
        )
        return 1
    print(result)
    return 0


def cmd_record_review(args):
    """Record one validated reviewer/test-reviewer round in the task JSON.

    Under the ingest lock it reads the task file strictly, checks the record
    against the task's existing review records, and atomically rewrites the
    task file. It never ingests telemetry, fetches pricing, regenerates the
    Markdown report, or changes usage/legacy/attribution data."""
    try:
        record = load_review_record_input(args.input)
    except ReviewRecordError as ex:
        print("Error: invalid review record: %s" % ex, file=sys.stderr)
        return 1
    task_id = record["task_id"]
    with ingest_lock():
        tpath = task_path(task_id)
        task, error = _load_task_file_strict(tpath, task_id)
        if error:
            print("Error: %s" % error, file=sys.stderr)
            return 1
        if task is None:
            # Conservative new-task file: only the identity and the review
            # block. Usage fields (created_at, totals, attribution, ...) are
            # created by the first real ingest, never by this command.
            task = {"task_id": task_id}
        try:
            outcome = apply_review_record(task, record)
        except ReviewRecordError as ex:
            print("Error: review record rejected: %s" % ex, file=sys.stderr)
            return 1
        if outcome == "duplicate":
            print("Review record '%s' is already recorded identically for task '%s'; nothing changed."
                  % (record["record_id"], task_id))
            return 0
        atomic_write(tpath, json.dumps(task, indent=2))
    print("Recorded review '%s' (task '%s', cycle '%s', %s round %d, verdict %s). The Markdown report is "
          "not regenerated by this command; run `copilot-task-report.py report %s` to refresh it."
          % (record["record_id"], task_id, record["cycle_id"], record["stage"], record["round"],
             record["verdict"], task_id))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Copilot task usage report helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_marker = sub.add_parser("ensure-marker", help="Idempotently create the install marker (call before the first-ever copilot run)")
    p_marker.set_defaults(func=cmd_ensure_marker)

    p_ingest = sub.add_parser("ingest", help="Ingest new telemetry for a session into its task report (also used for the pre-deletion ingest)")
    p_ingest.add_argument("--session-id", required=True)
    p_ingest.add_argument("--task", default="", help="task ID for this run (blank = reuse last known / UNASSIGNED)")
    p_ingest.add_argument("--session-dir", default="", help="Override session directory path")
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="Regenerate and print the report for a task")
    p_report.add_argument("task_id", help="task ID to report on (e.g. ABC-123, a free-form name, or the literal UNASSIGNED); "
                                            "malformed values are rejected with an error, never silently fixed up")
    p_report.set_defaults(func=cmd_report)

    p_norm = sub.add_parser("normalize-task-id", help="Validate/normalize a task ID or free-form task name (prints the normalized form; exit 1 if invalid)")
    p_norm.add_argument("raw", help="Raw task ID/name to normalize")
    p_norm.set_defaults(func=cmd_normalize)

    p_review = sub.add_parser(
        "record-review",
        help="Record one validated reviewer/test-reviewer round (orchestrator-supplied, not telemetry) "
             "in the task JSON; does not ingest, fetch pricing or regenerate the report")
    p_review.add_argument("--input", required=True, metavar="JSON_FILE",
                          help="Path to a review-record JSON file (schema %r, schema_version %d)"
                               % (REVIEW_RECORD_SCHEMA, REVIEW_RECORD_SCHEMA_VERSION))
    p_review.set_defaults(func=cmd_record_review)

    p_delivery = sub.add_parser(
        "record-delivery",
        help="Record one validated feature delivery event (started/completed/cancelled or a confirmed "
             "defect/regression; orchestrator-supplied, not telemetry) in the task JSON; does not ingest, "
             "fetch pricing or regenerate the report")
    p_delivery.add_argument("--input", required=True, metavar="JSON_FILE",
                            help="Path to a delivery-record JSON file (schema %r, schema_version %d)"
                                 % (DELIVERY_RECORD_SCHEMA, DELIVERY_RECORD_SCHEMA_VERSION))
    p_delivery.set_defaults(func=cmd_record_delivery)

    p_fcalls = sub.add_parser(
        "feature-calls",
        help="Read-only: print a session's call-ledger entries and invoke_agent groups as JSON, or with "
             "selectors plus --feature-id/--cycle-id a ready-to-save record-feature-usage payload. Writes "
             "nothing and never ingests, fetches pricing or uses the network")
    p_fcalls.add_argument("--task", required=True, help="task ID whose ledger to read")
    p_fcalls.add_argument("--session-id", required=True, help="session whose ledgered calls to show/select")
    p_fcalls.add_argument("--format", choices=["json"], default="json", help="output format (JSON only)")
    p_fcalls.add_argument("--invocation", action="append", metavar="TRACE_ID/SPAN_ID",
                          help="preview: select the ledgered calls whose direct parent is this invoke_agent span "
                               "(repeatable)")
    p_fcalls.add_argument("--call", action="append", metavar="TRACE_ID/SPAN_ID",
                          help="preview: select exactly this ledgered call (repeatable)")
    p_fcalls.add_argument("--feature-id", default=None, help="preview: feature the calls belong to")
    p_fcalls.add_argument("--cycle-id", default=None, help="preview: started delivery cycle of that feature")
    p_fcalls.add_argument("--record-id", default=None,
                          help="preview: record id (default: derived from feature, cycle and call set)")
    p_fcalls.add_argument("--basis", default=None, choices=list(FEATURE_USAGE_PROVENANCE_BASES),
                          help="preview: provenance basis (default orchestrator-selected)")
    p_fcalls.add_argument("--reference", default=None, help="preview: short provenance reference")
    p_fcalls.set_defaults(func=cmd_feature_calls, skip_migrations=True)

    p_fusage = sub.add_parser(
        "record-feature-usage",
        help="Record one explicit link of exact ledger calls to a started feature cycle, re-checking the "
             "previewed call count/fingerprint; does not ingest, fetch pricing or regenerate the report")
    p_fusage.add_argument("--input", required=True, metavar="JSON_FILE",
                          help="Path to a feature-usage-record JSON file (schema %r, schema_version %d), "
                               "normally saved from `feature-calls` preview output"
                               % (FEATURE_USAGE_RECORD_SCHEMA, FEATURE_USAGE_RECORD_SCHEMA_VERSION))
    p_fusage.set_defaults(func=cmd_record_feature_usage)

    p_ctx_update = sub.add_parser(
        "context-update",
        help="Enroll (--enroll) or refresh a copilot-s session's context-metrics sidecar and print a "
             "one-line summary; sessions without a sidecar are untouched unless --enroll is given. "
             "Never touches task reports, pricing or ingest state")
    p_ctx_update.add_argument("--session-id", required=True)
    p_ctx_update.add_argument("--session-dir", default="", help="Override session directory path")
    p_ctx_update.add_argument("--enroll", action="store_true",
                              help="Create the sidecar for a brand-new session copilot-s just launched")
    p_ctx_update.add_argument("--expect-cwd", default=None,
                              help="With --enroll: refuse unless workspace.yaml cwd equals this path")
    p_ctx_update.add_argument("--started-after", type=float, default=None, metavar="EPOCH",
                              help="With --enroll: refuse if workspace.yaml created_at predates this "
                                   "finite epoch, or is missing/unparseable")
    p_ctx_update.set_defaults(func=cmd_context_update, skip_migrations=True)

    p_ctx_list = sub.add_parser(
        "context-list",
        help="Read session ids from stdin; print one compact metrics label (or empty line) per id "
             "from existing sidecars only (read-only; never reads events logs)")
    p_ctx_list.add_argument("--session-state-dir", default="", help="Override the session-state directory")
    p_ctx_list.set_defaults(func=cmd_context_list, skip_migrations=True)

    args = parser.parse_args()
    try:
        # Context-metrics commands are read-only with respect to task-report
        # storage, so they deliberately skip the legacy storage migrations.
        if not getattr(args, "skip_migrations", False):
            run_startup_migrations()
        sys.exit(args.func(args) or 0)
    except Exception as ex:  # noqa: BLE001 - this tool must never crash the caller's shell
        print("copilot-task-report.py error: %s" % ex, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
