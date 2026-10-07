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
- Attribution (task["attribution"], versioned): model calls are NOT linked
  to agent invocations, because no supported identifier joins an OTEL chat
  span to a subagent invocation. Every new call is booked as "unknown" with
  an explicit reason; usage aggregated before the block existed on a task is
  frozen as "legacy". Calls are never attributed from timestamps, models or
  agent time windows, and no per-agent cost is computed (the official cost
  estimate is unchanged). The block also keeps an informational registry of
  invocations observed in events.jsonl (session id + agentId); registry
  presence is not ownership. The "By Custom Agent" self-report table is
  unchanged and separate.
- `record-review --input <file>` stores explicit, orchestrator-supplied
  reviewer/test-reviewer round records (task["reviews"]) after strict
  validation against the bounded review policy. They are not telemetry,
  and recording never ingests, fetches pricing or regenerates reports.
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
    "coder": "xhigh",
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
# not linked to an agent invocation. Today no supported link exists: the
# OTEL chat spans this tool reads carry the session's conversation id but no
# observed agent/invocation identifier, and calls are deliberately NEVER
# attributed from timestamps, models or "the only open interval" guesses.
# Every new call is therefore recorded as unknown with an explicit reason.
#
# Calls already aggregated on a task when the block is started are frozen as
# "legacy" (a snapshot of the task totals at that moment) — never re-derived,
# repriced or backfilled. The attribution buckets are exclusive and must
# reconcile with task["totals"]: legacy + unknown (+ attributed, currently
# always 0) == totals.
#
# The same block also holds an informational registry of agent invocations
# OBSERVED in events.jsonl (`subagent.started`/`subagent.completed`, keyed by
# session id + the event's own `agentId`). Registry presence is not ownership
# of any model call or cost.
ATTRIBUTION_SCHEMA_VERSION = 1
ATTRIBUTION_REASON_NO_SUPPORTED_LINK = (
    "no supported link: OTEL chat spans carry no observed agent/invocation identifier "
    "(timestamps, models and agent intervals are never used to attribute calls)"
)
ATTRIBUTION_PROVENANCE = {
    "model_calls": "OTEL 'chat *' spans matched by gen_ai.conversation.id == session id",
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


def attribution_reason_for_call(mc):
    """Why model call `mc` is (not) linked to an agent invocation.

    The only acceptable future link is an explicit invocation identifier
    actually present on the call's own telemetry record; none is observed on
    the OTEL chat spans read today, so every call returns the fixed
    "no supported link" reason. Timestamps, models and agent intervals are
    deliberately NOT used. Supporting an evidenced join later must add a
    separate attributed bucket under a new ATTRIBUTION_SCHEMA_VERSION."""
    return ATTRIBUTION_REASON_NO_SUPPORTED_LINK


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
        # Attribution delta: per-reason aggregates for calls that could not
        # be linked to an invocation (exclusive; every call lands in exactly
        # one reason) and observed invocation-registry events.
        "attribution": {"unknown": {}, "invocation_events": []},
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
    calls, min_ts, max_ts = scan_otel_files(session_id, sess_state, install_epoch)
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
        delta["attribution"]["unknown"].setdefault(reason, blank_agg())
        add_agg(delta["attribution"]["unknown"][reason], mc)
        delta["model_calls"] += 1

    delta["min_ts"] = min_ts
    delta["max_ts"] = max_ts

    delta["repositories"] = sorted(delta["repositories"])
    return delta


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
        # Delta from an older code path without attribution data: no call
        # can have a supported link, so book its calls under the same fixed
        # reason (keeps the buckets reconciled with the totals above).
        fallback = blank_agg()
        for agg in delta["by_model"].values():
            for k in blank_agg():
                fallback[k] += agg.get(k, 0)
        att_delta = {"unknown": {ATTRIBUTION_REASON_NO_SUPPORTED_LINK: fallback} if fallback["call_count"] else {},
                     "invocation_events": []}
    merge_attribution_delta(attribution, att_delta, session_id)
    return task


def prepare_attribution_block(task):
    """Return the task's v1 attribution block, starting it if absent.

    A newly started block freezes the task's CURRENT totals as "legacy" (all
    zero for a task with no recorded usage yet). An existing block of an
    unsupported shape/version is left untouched (never rewritten or
    "upgraded" by guessing) and None is returned, with a warning; the report
    then shows attribution as unreadable instead of a fabricated split."""
    att = task.get("attribution")
    if att is None:
        prior = task.get("totals") if isinstance(task.get("totals"), dict) else {}
        legacy = {}
        for k in blank_agg():
            value = prior.get(k, 0)
            legacy[k] = value if isinstance(value, int) and not isinstance(value, bool) else 0
        att = task["attribution"] = {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "since": now_iso(),
            "legacy": legacy,
            "unknown": {},
            "observed_invocations": {},
            "provenance": dict(ATTRIBUTION_PROVENANCE),
        }
        return att
    if (
        isinstance(att, dict)
        and att.get("schema_version") == ATTRIBUTION_SCHEMA_VERSION
        and isinstance(att.get("legacy"), dict)
        and isinstance(att.get("unknown"), dict)
        and isinstance(att.get("observed_invocations"), dict)
    ):
        return att
    print("Warning: task '%s' has an unsupported/malformed attribution block; leaving it unchanged "
          "(new calls are still counted in the task totals)." % task.get("task_id", "?"), file=sys.stderr)
    return None


def merge_attribution_delta(att, att_delta, session_id):
    """Merge one ingest's attribution delta into a v1 block (or do nothing if
    `att` is None — see prepare_attribution_block).

    Unknown-reason aggregates are additive. Registry merging is idempotent
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


def summarize_attribution(task):
    """Read-only summary of the attribution block for rendering.

    status: "active" (v1 block), "not_started" (task predates attribution
    tracking and has not been re-ingested: everything is shown as legacy),
    or "unreadable" (unsupported/malformed block; nothing is inferred)."""
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
    if not (isinstance(att, dict) and att.get("schema_version") == ATTRIBUTION_SCHEMA_VERSION
            and isinstance(att.get("legacy"), dict) and isinstance(att.get("unknown"), dict)
            and isinstance(att.get("observed_invocations"), dict)):
        out["status"] = "unreadable"
        out["reconciled"] = False
        return out
    out["since"] = att.get("since")
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
    "release status": None,
    "category": None,
    "tier": "tier",
    "threshold (input tokens)": "threshold",
    "input": "input",
    "cached input": "cached_input",
    "cache write": "cache_write",
    "output": "output",
}
_REQUIRED_COLUMNS = ("model", "input", "cached_input", "output")


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
    Raises OfficialPricingError on ANY inconsistency — the caller must then
    keep the previous cache rather than store a partial result."""
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
            rows = rows_by_model.setdefault(model_id, {"display_name": display, "rows": [], "notes": []})
            if rows["display_name"] != display:
                raise OfficialPricingError("%s: conflicting display names for id %r" % (where, model_id))
            rows["rows"].append({"tier": tier, "threshold": threshold, "rates": rates})
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


def _refresh_official_pricing(now):
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
                if 0 <= age < OFFICIAL_PRICING_TTL_SECONDS:
                    return {"attempted": False, "status": "fresh"}
            # Validated above: epochs are None or finite nonnegative numbers.
            last_fail = state.get("last_failure_epoch")
            last_ok = state.get("last_success_epoch") or 0
            if (last_fail is not None and last_fail > last_ok
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
            u = rec["unpriced"].setdefault(reason, {"calls": 0, "total_tokens": 0})
            u["calls"] += 1
            u["total_tokens"] += int(mc.get("total_tokens") or 0)
            continue
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
                  "coder/tester=xhigh, discovery=low; legacy fixer=high, kept only "
                  "for historical sessions recorded before the fixer role was retired "
                  "and folded into coder/senior-coder), "
                  "applied only when the call falls inside a fully-completed subagent "
                  "invocation window, and is **not** measured telemetry. `unknown` means no "
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
    lines.append("- Model calls are not attributed to agents: no supported telemetry link exists, so "
                  "calls are shown as unknown (or legacy) in Attribution Coverage — unknown does not "
                  "mean the main session made them. No per-agent cost is measured.")
    lines.append("- Recorded reviews are orchestrator-supplied records added with `record-review`, "
                  "not native telemetry, and recording them does not regenerate this report. A task "
                  "with no recorded reviews (including any work done before recording existed) is not "
                  "evidence of a skipped or failed review.")
    lines.append("")
    return "\n".join(lines)


def _md_cell(value):
    """Make a free-text value safe inside a Markdown table cell. Backslashes
    are escaped before pipes so an input like `a\\|b` cannot turn its own
    backslash into an escape that leaves a raw, cell-splitting pipe."""
    text = "" if value is None else str(value)
    return " ".join(text.split()).replace("\\", "\\\\").replace("|", "\\|")


def render_attribution_section(task):
    s = summarize_attribution(task)
    lines = ["## Attribution Coverage (model calls → agent invocations)", ""]
    if s["status"] == "unreadable":
        lines.append("_This task's attribution block has an unsupported or malformed schema; it is left "
                     "unchanged and not interpreted. Totals above are unaffected._")
        lines.append("")
        return lines
    lines.append("_Exclusive buckets that must add up to the Summary totals. **Unknown is not the "
                 "main session**: it means no supported link to an agent invocation exists. Calls are "
                 "never attributed from timestamps, models or agent time windows. No per-agent cost is "
                 "measured; the estimated USD cost above is unchanged and is not split by agent._")
    lines.append("")
    lines.append("| Bucket | Calls | Total Tokens |")
    lines.append("|---|---|---|")
    if s["status"] == "not_started":
        legacy_label = "Legacy — recorded before attribution tracking (starts at the next ingest)"
    else:
        legacy_label = "Legacy — recorded before attribution tracking started (%s)" % (s["since"] or "unknown")
    lines.append("| %s | %d | %d |" % (legacy_label, s["legacy_calls"], s["legacy_tokens"]))
    for reason, row in sorted(s["unknown"].items()):
        lines.append("| Unknown — %s | %d | %d |" % (_md_cell(reason), row["calls"], row["total_tokens"]))
    lines.append("| Attributed to an observed agent invocation | %d | %d |"
                 % (s["attributed_calls"], s["attributed_tokens"]))
    lines.append("| **Total (Summary)** | %d | %d |" % (s["total_calls"], s["total_tokens"]))
    lines.append("")
    if not s["reconciled"]:
        lines.append("> ⚠️ Attribution buckets do not reconcile with the Summary totals (legacy + unknown "
                     "+ attributed ≠ totals); treat this section as incomplete for this task.")
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
        ):
            # Still persist offsets/cursors so we don't rescan the same
            # bytes forever, but nothing to merge into the task. Every
            # mergeable delta field must be checked here — a premium-only,
            # repository-only or invocation-registry-only update (e.g. a
            # bare subagent.started with no model calls yet) would
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

    args = parser.parse_args()
    try:
        run_startup_migrations()
        sys.exit(args.func(args) or 0)
    except Exception as ex:  # noqa: BLE001 - this tool must never crash the caller's shell
        print("copilot-task-report.py error: %s" % ex, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
