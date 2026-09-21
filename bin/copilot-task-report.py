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
"""

import argparse
import contextlib
import filecmp
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
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
LOCK_FILE = os.path.join(SUPPORT_DIR, ".ingest.lock")
REPORTS_DIR = os.environ.get(
    "COPILOT_TASK_REPORTS_DIR",
    os.path.join(HOME, "Desktop", "CopilotTaskReports"),
)
SESSION_STATE_DIR = os.path.join(HOME, ".copilot", "session-state")
OTEL_DIR = os.path.join(HOME, ".copilot", "otel")

UNASSIGNED = "UNASSIGNED"
TASK_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")

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
# Delta building / merging into the per-task aggregate
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
    return task


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

    total_usd, priced, unpriced = estimate_usd(task.get("by_model", {}))
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
    for model, agg in sorted(task.get("by_model", {}).items(), key=lambda kv: -kv[1]["total_tokens"]):
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


def task_path(task_id):
    safe = task_id.replace("/", "_")
    return os.path.join(TASKS_DIR, "%s.json" % safe)


def report_path(task_id):
    safe = task_id.replace("/", "_")
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

        # --task may be blank (reuse last known) or invalid (safely fall
        # back rather than silently accepting garbage as a task ID).
        requested = normalize_task_id(args.task) if args.task else None
        if args.task and requested is None:
            print("Warning: ignoring invalid task ID '%s' from caller; falling back to "
                  "last-known/UNASSIGNED." % args.task, file=sys.stderr)
        task_id = requested or sess_state.get("task_id") or UNASSIGNED
        sess_state["task_id"] = task_id

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
            # bytes forever, but nothing to merge into the task. Every
            # mergeable delta field must be checked here — a premium-only
            # or repository-only update (no new model calls/AIU/agent
            # summaries) would otherwise be silently dropped even though
            # build_delta's advanced offsets mean it can never be
            # re-derived on a later ingest.
            atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))
            return 0

        # Crash-safety ordering: persist the advanced offsets/cursors FIRST.
        # If we crash before the task write below, the worst case is an
        # undercounted task (safe to re-run later without double
        # counting) rather than a double-counted one.
        atomic_write(STATE_FILE, json.dumps(state, indent=2, default=list))

        task = load_json(task_path(task_id), {})
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

    args = parser.parse_args()
    try:
        run_startup_migrations()
        sys.exit(args.func(args) or 0)
    except Exception as ex:  # noqa: BLE001 - this tool must never crash the caller's shell
        print("copilot-task-report.py error: %s" % ex, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
