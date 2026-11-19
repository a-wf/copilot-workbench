#!/usr/bin/env python3
"""
copilot-session-report.py — per-launch session reports for copilot-s.

Every `copilot` launch made by copilot-s gets one run record (a fresh random
run id), in either mode:

  normal  — plain Copilot, exactly as before (baseline);
  rtk     — `copilot-s --rtk`: a session-local hook (bin/copilot-s-rtk-plugin,
            loaded only through `--plugin-dir` for that one launch) rewrites a
            small allowlist of simple test/build commands to `rtk <command>`.

Run records live under ~/.copilot/session-reports/runs/<run-id>/run.json
(override the root with COPILOT_SESSION_REPORTS_DIR). Directories are 0700,
files 0600, and every write is atomic. A record holds METADATA ONLY: ids,
versions, states, timestamps, exit status, call/token counts, frozen estimated
costs and content-free RTK aggregates. It never contains prompts, commands,
arguments, tool output or file paths of the work.

Raw data owned by other tools stays in the run's private `raw/` folder and is
disclosed, never copied into a report: RTK's own history database (it stores
raw command lines and project paths), RTK tee/recall stores (raw output RTK
elided or kept for failed commands) and Copilot's `--usage-output-file` JSON
(may list modified file paths). Nothing is ever deleted automatically.

Sources and rules
- Model calls/tokens: this launch's OWN OTEL file (copilot-s gives every launch
  a unique `copilot-otel-<timestamp>-<run-id>.jsonl`), `chat *` spans whose
  `gen_ai.conversation.id` is exactly the linked session id, de-duplicated by
  (traceId, spanId). Spans without stable ids are counted, never guessed.
- Session link: the session copilot-s resumed, or the single new session
  directory it detected, cross-checked against hook evidence (RTK mode) and
  the conversation ids in the launch's own OTEL file; with no launcher
  candidate, a single conversation id in the launch's own OTEL file is used.
  Any conflict or ambiguity leaves the session UNKNOWN (never "the latest").
- Estimated cost: priced once, at finalization, with copilot-task-report.py's
  validated official GitHub per-token snapshot (cache only — no network), and
  frozen with the snapshot id/date. Read commands never reprice. Unknown
  models/usage shapes are reported as unpriced (partial), never $0.
- RTK statistics: `rtk gain --format json` against the launch's private,
  isolated RTK database only (never the global one, never a before/after
  diff); only the numeric summary fields are kept.
- Avoided-token value: RTK's own token estimate x the official input rate(s)
  of this run's priced calls — one exposure, as fresh input; cached or
  repeated context is not accounted. Several models give a min..max range.
  An estimate, never a bill or a credit deduction.

Read-only, deterministic commands: `launch`, `session`, `compare`.
Writers: `start`, `finish`, `mark-outcome` (per-run lock, atomic).

Automatic public RTK publication (no manual step): one numeric-only file per
RTK-mode launch at ~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json
(0700/0600, atomic, validated before publication) for optional viewers such
as Agentic Watcher. While a `copilot-s --rtk` launch runs, the launcher's
launch-scoped `rtk-publisher` refreshes it about every 30 s (one bounded
`rtk gain` at a time on the launch's private database); `finish` publishes the
stored final figures; `start` reconciles finalized runs in a bounded,
resumable slice (no RTK). Run records and reports are never changed.

Only an explicit `copilot-report-cleanup.py apply`/`restore --yes` (copilot-s
menu `r`) moves finalized run folders and their publications to/from a
recoverable quarantine, holding the reconcile lock, the run lock and the
capacity lock in that order; every writer here re-reads the record by its
canonical path after taking the run lock, so a moved run is never written
to or republished.
"""

import argparse
import contextlib
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HELPER_VERSION = "1.0.0"
RUN_SCHEMA = "copilot-s.session-run"
RUN_SCHEMA_VERSION = 1
SUPPORTED_RTK_VERSION = "1.2.0"

MODES = ("normal", "rtk")
LAUNCH_KINDS = ("new", "resume")
STATE_INCOMPLETE = "incomplete"
STATES = (STATE_INCOMPLETE, "completed", "copilot-error", "interrupted", "report-failure")
OUTCOMES = ("unknown", "completed", "abandoned")
USAGE_FILE_OWNERS = ("owned", "caller")

RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
OTEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,39}$")
HOOK_REASON_RE = re.compile(r"^[a-z0-9-]{1,40}$")
LABEL_RE = re.compile(r"^[\x20-\x7e]{1,200}$")
RTK_HOOK_REF_RE = re.compile(r"\brtk(?:\.exe)?[\"']?\s+hook\b")

MAX_INT = 2 ** 53
OTEL_MAX_LINE_BYTES = 4 * 1024 * 1024
HOOK_LOG_MAX_BYTES = 64 * 1024 * 1024
HOOK_LOG_MAX_LINE = 4096
SCAN_FILE_MAX_BYTES = 1024 * 1024
NATIVE_USAGE_MAX_BYTES = 4 * 1024 * 1024
NATIVE_USAGE_MAX_MODELS = 50
RTK_TIMEOUT_SECONDS = 15
RTK_OUTPUT_MAX_BYTES = 1024 * 1024
EVENTS_VERSION_SKEW_SECONDS = 5

RTK_RUN_ENV_KEYS = ("RTK_DB_PATH", "RTK_TEE_DIR", "RTK_RECALL_DB", "RTK_CONFIG_DIR", "RTK_TELEMETRY_DISABLED")
RTK_SUMMARY_INT_KEYS = ("total_commands", "total_input", "total_output", "total_saved", "total_time_ms",
                        "avg_time_ms", "oversized_excluded")
HOOK_DECISIONS = ("rewritten", "unchanged", "denied")

# User-level RTK files that could change what RTK does in this launch and
# that copilot-s cannot redirect without changing HOME (which would break the
# commands RTK runs). Their presence refuses RTK mode.
RTK_EXTERNAL_FILES = (
    ("config.toml", "RTK user configuration (filters, hooks, permission rules)"),
    ("filters.toml", "RTK user-global custom filters"),
    ("trusted_filters.json", "RTK trust store (would let project-local filters/pipelines load)"),
)

PRIVATE_RAW_CONTENTS = {
    "normal": [
        "copilot-usage.json: Copilot's --usage-output-file for this launch (may list modified file paths)",
    ],
    "rtk": [
        "copilot-usage.json: Copilot's --usage-output-file for this launch (may list modified file paths)",
        "rtk/history.db: RTK's own tracking database (stores raw command lines and project paths)",
        "rtk/tee/ and rtk/recall.db: RTK tee/recall stores (raw command output RTK elided or kept for failed commands)",
        "rtk/config/: empty RTK pipeline directory (isolates user pipelines and delegation tasks)",
        "rtk-preflight/: RTK database of the launch preflight smoke test",
    ],
}

NOTE_NOT_A_BILL = ("Estimated with official GitHub per-token list prices; not a bill, not a credit deduction, "
                   "and not your organization's actual charge.")
NOTE_OBSERVATIONAL = ("Normal vs RTK figures are observational: runs differ in task, prompts and model routing, "
                      "so differences are not causal and small samples are not comparable.")
NOTE_COVERAGE = ("Only launches made through copilot-s with session reports are covered; earlier launches and "
                 "launches outside copilot-s are not included and are never backfilled.")


class ReportError(Exception):
    pass


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_epoch():
    return time.time()


def iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strict_count(value):
    """A finite non-negative integer count <= 2**53, else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_INT:
        return None
    return value


def strict_signed(value):
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_INT:
        return None
    return value


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def safe_label(value):
    return value if isinstance(value, str) and LABEL_RE.match(value) else None


def valid_session_id(value):
    return isinstance(value, str) and bool(SESSION_ID_RE.match(value))


def reports_root():
    override = os.environ.get("COPILOT_SESSION_REPORTS_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".copilot", "session-reports")


def runs_dir():
    return os.path.join(reports_root(), "runs")


def run_dir(run_id):
    return os.path.join(runs_dir(), run_id)


def ensure_private_dir(path):
    """Create (if needed) a 0700 directory owned by us; refuse symlinks."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ReportError("%s is not a plain directory" % path)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ReportError("%s is not owned by the current user" % path)
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def atomic_write_json(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)


def read_regular_file(path, max_bytes):
    """Bytes of a regular (non-symlink) file, or None if absent/unsuitable."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
        return None
    with open(path, "rb") as f:
        return f.read(max_bytes + 1)[:max_bytes]


@contextlib.contextmanager
def run_lock(rdir, wait=10):
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    path = os.path.join(rdir, ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReportError("run record is busy (another copilot-s process holds its lock)")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def load_run(run_id):
    if not RUN_ID_RE.match(run_id or ""):
        raise ReportError("invalid run id")
    data = read_regular_file(os.path.join(run_dir(run_id), "run.json"), 8 * 1024 * 1024)
    if data is None:
        raise FileNotFoundError(run_id)
    rec = json.loads(data.decode("utf-8"))
    if (not isinstance(rec, dict) or rec.get("schema") != RUN_SCHEMA
            or rec.get("schema_version") != RUN_SCHEMA_VERSION or rec.get("run_id") != run_id):
        raise ReportError("run record %s has an unsupported schema" % run_id)
    return rec


def iter_runs():
    """(records, skipped_count) for every readable run record, sorted by
    creation time then run id (deterministic)."""
    out, skipped = [], 0
    try:
        names = sorted(os.listdir(runs_dir()))
    except OSError:
        return out, skipped
    for name in names:
        if not RUN_ID_RE.match(name):
            continue
        try:
            out.append(load_run(name))
        except (OSError, ValueError, ReportError, FileNotFoundError):
            skipped += 1
    out.sort(key=lambda r: (r.get("created_epoch") or 0, r["run_id"]))
    return out, skipped


def sanitized_rtk_env(rtk_env):
    """Current environment without any inherited RTK_* variable, plus the
    launch's own isolated RTK paths."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RTK_")}
    env.update(rtk_env)
    return env


def rtk_env_for(rdir, sub="rtk"):
    base = os.path.join(rdir, "raw", sub)
    return {
        "RTK_DB_PATH": os.path.join(base, "history.db"),
        "RTK_TEE_DIR": os.path.join(base, "tee"),
        "RTK_RECALL_DB": os.path.join(base, "recall.db"),
        "RTK_CONFIG_DIR": os.path.join(base, "config"),
        "RTK_TELEMETRY_DISABLED": "1",
    }


def run_capture(argv, env, cwd, timeout, stdin_bytes=None):
    """(returncode, stdout_bytes) or raises ReportError with a short code."""
    try:
        proc = subprocess.run(argv, input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=env, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ReportError("timed out after %ss" % timeout)
    except OSError as ex:
        raise ReportError("could not run (%s)" % type(ex).__name__)
    if len(proc.stdout) > RTK_OUTPUT_MAX_BYTES:
        raise ReportError("output too large")
    return proc.returncode, proc.stdout


# --------------------------------------------------------------------------
# RTK preflight
# --------------------------------------------------------------------------

def rtk_user_dirs():
    """RTK's user config/data directories on this platform (mirrors the
    `dirs` crate RTK 1.2.0 uses: config_dir()/data_local_dir() + 'rtk')."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "rtk")]
    if os.name == "nt":
        out = []
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                out.append(os.path.join(base, "rtk"))
        return out
    xc = os.environ.get("XDG_CONFIG_HOME")
    xd = os.environ.get("XDG_DATA_HOME")
    config = os.path.join(xc if xc and os.path.isabs(xc) else os.path.join(home, ".config"), "rtk")
    data = os.path.join(xd if xd and os.path.isabs(xd) else os.path.join(home, ".local", "share"), "rtk")
    return sorted(set([config, data]))


def git_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def project_roots(project_dirs):
    """(roots, problems) for the directories a session may use. Each existing
    directory contributes its lexical absolute path AND its realpath (a
    symlinked launch/-C/--add-dir path must not hide the real repository),
    plus the git root of both. Only these known directories are resolved —
    config files inside them are never followed through symlinks. A missing
    directory is a problem (cannot be verified), never skipped silently."""
    roots, problems = [], []
    for d in project_dirs:
        lexical = os.path.abspath(d)
        if not os.path.isdir(lexical):
            problems.append((lexical, "session project directory does not exist; cannot verify its hook "
                                      "configuration is RTK-free"))
            continue
        for base in (lexical, os.path.realpath(lexical)):
            for root in (base, git_root(base)):
                if root and root not in roots:
                    roots.append(root)
    return roots, problems


# Per-project-root hook configuration files Copilot CLI reads (1.0.114).
PROJECT_HOOK_FILES = (
    (".claude", "settings.json"),
    (".claude", "settings.local.json"),
    (".github/copilot", "settings.json"),
    (".github/copilot", "settings.local.json"),
)

HOOK_SCAN_MAX_ENTRIES = 20000
WHY_RTK_HOOK = ("an existing Copilot hook configuration already invokes `rtk hook` (it would rewrite commands "
                "outside copilot-s and contaminate both modes)")


def _classify_hook_candidate(path):
    """None if the candidate is absent or verified free of `rtk hook`, else a
    refusal reason. Never follows a symlink and never reads past the size
    bound: anything that cannot be fully verified is refused, not assumed
    absent."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as ex:
        return "hook configuration candidate cannot be inspected (%s); cannot verify it is RTK-free" % (
            type(ex).__name__)
    if stat.S_ISLNK(st.st_mode):
        return "hook configuration candidate is a symlink (not followed); cannot verify it is RTK-free"
    if not stat.S_ISREG(st.st_mode):
        return "hook configuration candidate is not a regular file; cannot verify it is RTK-free"
    if st.st_size > SCAN_FILE_MAX_BYTES:
        return "hook configuration candidate exceeds %d bytes; cannot verify it is RTK-free" % SCAN_FILE_MAX_BYTES
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            data = f.read(SCAN_FILE_MAX_BYTES + 1)
    except OSError as ex:
        return "hook configuration candidate is unreadable (%s); cannot verify it is RTK-free" % type(ex).__name__
    if len(data) > SCAN_FILE_MAX_BYTES:
        return "hook configuration candidate exceeds %d bytes; cannot verify it is RTK-free" % SCAN_FILE_MAX_BYTES
    if RTK_HOOK_REF_RE.search(data.decode("utf-8", errors="replace")):
        return WHY_RTK_HOOK
    return None


def _list_hook_dir(path, problems):
    """*.json entries of a hooks directory, or [] if absent. A symlinked or
    unlistable directory is recorded as a problem (never followed)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as ex:
        problems.append((path, "hooks directory cannot be inspected (%s); cannot verify it is RTK-free"
                         % type(ex).__name__))
        return []
    if stat.S_ISLNK(st.st_mode):
        problems.append((path, "hooks directory is a symlink (not followed); cannot verify it is RTK-free"))
        return []
    if not stat.S_ISDIR(st.st_mode):
        return []
    try:
        names = sorted(os.listdir(path))
    except OSError as ex:
        problems.append((path, "hooks directory is unreadable (%s); cannot verify it is RTK-free"
                         % type(ex).__name__))
        return []
    return [os.path.join(path, n) for n in names if n.endswith(".json")]


def _installed_plugin_problems(plugins):
    """Installed plugins can declare hooks anywhere (plugin.json /
    .claude-plugin/plugin.json inline hooks, custom manifest hook paths,
    hooks/*.json), so EVERY file of the installed-plugins tree is checked
    with the same bounded, symlink-safe scan as caller plugins: any file
    mentioning `rtk hook` or anything unverifiable (symlinks, non-regular,
    unreadable or over-1 MiB files, too many entries, nesting beyond the
    depth cap) is refused, never assumed absent."""
    try:
        st = os.lstat(plugins)
    except FileNotFoundError:
        return []
    except OSError as ex:
        return [(plugins, "installed plugins cannot be inspected (%s); %s" % (type(ex).__name__, UNVERIFIED))]
    if stat.S_ISLNK(st.st_mode):
        return [(plugins, "installed plugins directory is a symlink (not followed); %s" % UNVERIFIED)]
    if not stat.S_ISDIR(st.st_mode):
        return [(plugins, "installed plugins path is not a directory; %s" % UNVERIFIED)]
    return _scan_plugin_tree(plugins, "installed plugin", INSTALLED_PLUGINS_MAX_DEPTH)


def existing_rtk_hook_configs(project_dirs):
    """(path, reason) for every Copilot hook configuration (project roots of
    every directory the session may run in or add — lexical and real paths
    and their git roots: .github/hooks/*.json, .claude/settings*.json,
    .github/copilot/settings*.json — user
    settings/hooks, installed plugins) that already invokes `rtk hook ...` —
    it would rewrite commands in BOTH modes and double-process RTK-mode
    commands — or that cannot be verified (symlink, non-regular, oversized,
    unreadable, missing project directory). Paths and reasons only; content
    is never kept."""
    candidates = []
    roots, problems = project_roots(project_dirs)
    for root in roots:
        # Config directories are never followed through a symlink.
        for sub in (".github", ".github/copilot", ".claude"):
            path = os.path.join(root, *sub.split("/"))
            if os.path.islink(path):
                problems.append((path, "project configuration directory is a symlink (not followed); cannot "
                                       "verify its hook configuration is RTK-free"))
        candidates.extend(_list_hook_dir(os.path.join(root, ".github", "hooks"), problems))
        for sub, name in PROJECT_HOOK_FILES:
            candidates.append(os.path.join(root, *sub.split("/"), name))
    chome = os.environ.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
    for name in ("settings.json", "config.json"):
        candidates.append(os.path.join(chome, name))
    candidates.extend(_list_hook_dir(os.path.join(chome, "hooks"), problems))
    problems.extend(_installed_plugin_problems(os.path.join(chome, "installed-plugins")))
    for path in candidates:
        why = _classify_hook_candidate(path)
        if why is not None:
            problems.append((path, why))
    return problems


PLUGIN_SCAN_MAX_DEPTH = 8
INSTALLED_PLUGINS_MAX_DEPTH = 10  # <marketplace>/<plugin>/ + a plugin's own depth
UNVERIFIED = "cannot verify it does not run `rtk hook`"


def caller_plugin_problems(raw_dirs, cwd, cwd_uncertain, own_plugin_dir):
    """(path, reason) refusals for caller-supplied `--plugin-dir` operands in
    RTK mode. Each plugin tree is walked read-only and bounded (entries,
    depth, 1 MiB per file), never following symlinks: any file mentioning
    `rtk hook`, and anything that cannot be fully verified (missing,
    symlinked, non-regular or oversized entries, unreadable directories,
    relative paths whose base directory is uncertain), is refused. Plugins
    are never modified."""
    problems = []
    own = os.path.realpath(own_plugin_dir) if own_plugin_dir else None
    for raw in raw_dirs or []:
        if not raw:
            problems.append(("--plugin-dir", "caller --plugin-dir has no directory operand; %s" % UNVERIFIED))
            continue
        if os.path.isabs(raw):
            path = os.path.normpath(raw)
        elif cwd_uncertain:
            problems.append((raw, "relative caller --plugin-dir with -C/--worktree or a resumed/selected "
                                  "session: Copilot may resolve it against another directory; pass an "
                                  "absolute path"))
            continue
        else:
            path = os.path.normpath(os.path.join(os.path.abspath(cwd), raw))
        try:
            st = os.lstat(path)
        except OSError:
            problems.append((path, "caller --plugin-dir does not exist or cannot be inspected; %s" % UNVERIFIED))
            continue
        if stat.S_ISLNK(st.st_mode):
            problems.append((path, "caller --plugin-dir is a symlink (not followed); %s" % UNVERIFIED))
            continue
        if not stat.S_ISDIR(st.st_mode):
            problems.append((path, "caller --plugin-dir is not a directory; %s" % UNVERIFIED))
            continue
        if own and os.path.realpath(path) == own:
            problems.append((path, "the copilot-s RTK hook plugin is already loaded by --rtk; loading it twice "
                                   "would double-process commands"))
            continue
        problems.extend(_scan_plugin_tree(path, "caller plugin", PLUGIN_SCAN_MAX_DEPTH))
    return problems


def _scan_plugin_tree(root, label, max_depth):
    """Bounded, read-only, symlink-safe check of every file under `root`."""
    problems = []
    seen = 0
    base_depth = root.rstrip(os.sep).count(os.sep)

    def onerror(ex):
        problems.append((getattr(ex, "filename", root) or root,
                         "%s directory is unreadable (%s); %s" % (label, type(ex).__name__, UNVERIFIED)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        seen += len(dirnames) + len(filenames)
        if seen > HOOK_SCAN_MAX_ENTRIES:
            problems.append((root, "%s tree has more than %d entries; %s" % (label, HOOK_SCAN_MAX_ENTRIES,
                                                                             UNVERIFIED)))
            return problems
        if dirpath.count(os.sep) - base_depth >= max_depth and dirnames:
            problems.append((dirpath, "%s tree nests deeper than %d levels; %s" % (label, max_depth, UNVERIFIED)))
            return problems
        for name in sorted(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                problems.append((os.path.join(dirpath, name), "symlinked directory in the %s tree (not "
                                                              "followed); %s" % (label, UNVERIFIED)))
        dirnames.sort()
        for name in sorted(filenames):
            why = _classify_hook_candidate(os.path.join(dirpath, name))
            if why is not None:
                problems.append((os.path.join(dirpath, name), "%s file: %s" % (label, why)))
    return problems


def session_project_dirs(cwd, project_dirs):
    """Directories the session may run in: the launcher-resolved ones
    (--project-dir: menu-resume recorded cwd, -C target, $PWD), else cwd."""
    dirs = [os.path.abspath(d) for d in (project_dirs or []) if d]
    return dirs or [os.path.abspath(cwd)]


def rtk_static_checks(cwd, plugin_dir, extra_plugin_dirs=None, cwd_uncertain=False, project_dirs=None,
                      project_dir_unknown=None):
    """Read-only checks. Returns (info, refusals) — refusals are
    (path_or_item, why) pairs; nothing sensitive is read into them."""
    refusals = []
    info = {"supported_version": SUPPORTED_RTK_VERSION}
    found = shutil.which("rtk")
    if not found:
        refusals.append(("rtk", "the `rtk` command was not found on PATH"))
        return info, refusals
    rtk_bin = os.path.realpath(found)
    info["rtk_bin"] = rtk_bin
    env = {k: v for k, v in os.environ.items() if not k.startswith("RTK_")}
    env["RTK_TELEMETRY_DISABLED"] = "1"
    try:
        rc, out = run_capture([rtk_bin, "--version"], env, None, RTK_TIMEOUT_SECONDS)
    except ReportError as ex:
        refusals.append((rtk_bin, "`rtk --version` failed: %s" % ex))
        return info, refusals
    text = out.decode("utf-8", errors="replace").strip()
    version = text[4:] if text.startswith("rtk ") else ""
    if rc != 0 or not VERSION_RE.match(version or ""):
        refusals.append((rtk_bin, "`rtk --version` did not report a version"))
    else:
        info["rtk_version"] = version
        if version != SUPPORTED_RTK_VERSION:
            refusals.append((rtk_bin, "RTK %s is not supported (only %s was verified with this Copilot hook "
                                      "integration)" % (version, SUPPORTED_RTK_VERSION)))
    for d in rtk_user_dirs():
        for name, why in RTK_EXTERNAL_FILES:
            p = os.path.join(d, name)
            if os.path.lexists(p):
                refusals.append((p, why))
    dirs = session_project_dirs(cwd, project_dirs)
    if project_dir_unknown:
        refusals.append(("session directory", "copilot-s cannot determine which directory this session will run "
                                              "in (%s), so its project hook configuration cannot be verified "
                                              "RTK-free" % project_dir_unknown))
    refusals.extend(existing_rtk_hook_configs(dirs))
    refusals.extend(caller_plugin_problems(extra_plugin_dirs, cwd, cwd_uncertain, plugin_dir))
    project_rtk = []
    roots, _problems = project_roots(dirs)
    for root in roots:
        if os.path.lexists(os.path.join(root, ".rtk")):
            project_rtk.append(root)
    info["project_rtk_dir"] = ("present (not loaded: no RTK trust store exists, so project filters and "
                               "pipelines stay untrusted)" if project_rtk else "absent")
    if plugin_dir is not None:
        if not os.path.isabs(plugin_dir):
            refusals.append((plugin_dir, "the RTK hook plugin directory must be an absolute path"))
        else:
            for rel in (os.path.join(".claude-plugin", "plugin.json"), os.path.join("hooks", "hooks.json"),
                        os.path.join("hooks", "copilot-s-rtk-hook.py")):
                if not os.path.isfile(os.path.join(plugin_dir, rel)):
                    refusals.append((os.path.join(plugin_dir, rel), "copilot-s RTK hook plugin file is missing "
                                                                     "(re-run scripts/install.sh)"))
    return info, refusals


def rtk_smoke_test(rtk_bin, rdir):
    """Run the real `rtk hook copilot` on two synthetic commands with an
    isolated preflight database. Returns a list of failure strings."""
    env_paths = rtk_env_for(rdir, "rtk-preflight")
    for key in ("RTK_TEE_DIR", "RTK_CONFIG_DIR"):
        ensure_private_dir(env_paths[key])
    env = sanitized_rtk_env(env_paths)
    cwd = os.path.join(rdir, "raw", "rtk-preflight")
    failures = []
    cases = (("cargo test", "rtk cargo test"), ("echo copilot-s-preflight", None))
    for command, expected in cases:
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": command}}).encode("utf-8")
        try:
            rc, out = run_capture([rtk_bin, "hook", "copilot"], env, cwd, RTK_TIMEOUT_SECONDS, payload)
        except ReportError as ex:
            failures.append("`rtk hook copilot` %s" % ex)
            continue
        if rc != 0:
            failures.append("`rtk hook copilot` exited %d" % rc)
            continue
        text = out.decode("utf-8", errors="replace").strip()
        if expected is None:
            if text:
                failures.append("`rtk hook copilot` rewrote a command it should leave unchanged")
            continue
        try:
            hso = json.loads(text).get("hookSpecificOutput") or {}
        except (ValueError, AttributeError):
            failures.append("`rtk hook copilot` answered unparseably")
            continue
        got = (hso.get("updatedInput") or {}).get("command") if isinstance(hso, dict) else None
        if got != expected or "permissionDecision" in hso:
            failures.append("`rtk hook copilot` did not return the expected plain rewrite")
    return failures


def print_refusals(refusals):
    print("copilot-s: RTK mode refused — nothing was launched.", file=sys.stderr)
    for item, why in refusals:
        print("  - %s: %s" % (item, why), file=sys.stderr)
    print("Fix the items above (or run without --rtk). copilot-s never edits RTK or Copilot configuration "
          "for you.", file=sys.stderr)


# --------------------------------------------------------------------------
# start / finish
# --------------------------------------------------------------------------

def new_record(a, rdir):
    created = now_epoch()
    return {
        "schema": RUN_SCHEMA,
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": a.run_id,
        "mode": a.mode,
        "state": STATE_INCOMPLETE,
        "state_reason": "launch-pending",
        "copilot_outcome": None,
        "created_at": iso(created),
        "created_epoch": round(created, 3),
        "launched_at": None,
        "ended_at": None,
        "finalized_at": None,
        "wall_clock_ms": None,
        "exit_code": None,
        "launch": {"kind": a.launch_kind, "caller_selects_session": bool(a.caller_selects_session),
                   "resume_session_id": a.resume_session_id or None},
        "session": {"id": None, "status": "unknown", "basis": None, "reason": "not finalized"},
        "versions": {"copilot_s": safe_label(a.copilot_s_version), "session_report_helper": HELPER_VERSION,
                     "copilot": None, "rtk": None},
        "capabilities": {
            "otel_file": bool(a.otel_file),
            "native_usage_file": a.usage_file_owner,
            "rtk_hook": a.mode == "rtk",
            "rtk_stats_isolated": a.mode == "rtk",
        },
        "otel": {"file": a.otel_file or None},
        "task_outcome": {"value": "unknown", "source": "default", "marked_at": None},
        "usage": None,
        "cost": None,
        "native_usage": None,
        "rtk": None,
        "baseline_notes": [],
        "privacy": {
            "report_contains": ("metadata only: ids, versions, states, times, exit status, counts, token totals, "
                                "estimated costs and RTK numeric aggregates; never prompts, commands, arguments, "
                                "tool output or work file paths"),
            "private_raw_store": {
                "path": os.path.join(rdir, "raw"),
                "contents": PRIVATE_RAW_CONTENTS[a.mode],
                "permissions": ("run folders are 0700 and files copilot-s writes are 0600; files RTK or Copilot create "
                                "inside are protected by those 0700 folders"),
                "retention": "kept until you delete it; copilot-s never deletes it automatically",
            },
        },
    }


def cmd_preflight(a):
    info, refusals = rtk_static_checks(a.cwd, a.plugin_dir, a.extra_plugin_dir, a.session_cwd_uncertain,
                                       a.project_dir, a.project_dir_unknown)
    if refusals:
        print_refusals(refusals)
        return 3
    print("RTK %s at %s — preflight OK" % (info.get("rtk_version"), info.get("rtk_bin")))
    return 0


def cmd_start(a):
    if not RUN_ID_RE.match(a.run_id):
        raise ReportError("invalid run id")
    if a.resume_session_id and not valid_session_id(a.resume_session_id):
        raise ReportError("invalid resume session id")
    if a.launch_kind == "resume" and not a.resume_session_id:
        raise ReportError("--launch-kind resume needs --resume-session-id")
    info, refusals = (None, [])
    if a.mode == "rtk":
        info, refusals = rtk_static_checks(a.cwd, a.plugin_dir, a.extra_plugin_dir, a.session_cwd_uncertain,
                                           a.project_dir, a.project_dir_unknown)
        if refusals:
            print_refusals(refusals)
            return 3
    ensure_private_dir(reports_root())
    ensure_private_dir(runs_dir())
    rdir = run_dir(a.run_id)
    os.mkdir(rdir, 0o700)  # never reuse an existing run id
    ensure_private_dir(rdir)
    ensure_private_dir(os.path.join(rdir, "raw"))
    rec = new_record(a, rdir)
    lines = [("RUN_DIR", rdir)]
    if a.usage_file_owner == "owned":
        usage_path = os.path.join(rdir, "raw", "copilot-usage.json")
        rec["native_usage"] = {"availability": "pending", "file": usage_path}
        lines.append(("USAGE_FILE", usage_path))
    else:
        rec["native_usage"] = {"availability": "unavailable",
                               "reason": "the caller passed its own --usage-output-file; copilot-s does not "
                                         "read or override a caller-owned path"}
    # Normal mode also records whether an RTK hook outside copilot-s could be
    # rewriting commands (baseline contamination); paths are not stored.
    if a.mode == "normal":
        try:
            contaminated = bool(a.project_dir_unknown) or bool(
                existing_rtk_hook_configs(session_project_dirs(a.cwd, a.project_dir)))
        except OSError:
            contaminated = True
        if contaminated:
            rec["baseline_notes"].append("an existing Copilot hook configuration invokes `rtk hook`, or the "
                                         "session directory / its hook configuration could not be verified "
                                         "RTK-free; this normal-mode run may not be an RTK-free baseline")
    with run_lock(rdir):
        atomic_write_json(os.path.join(rdir, "run.json"), rec)
        if a.mode == "rtk":
            inherited = sorted(k for k in os.environ if k.startswith("RTK_"))
            env_paths = rtk_env_for(rdir)
            for key in ("RTK_TEE_DIR", "RTK_CONFIG_DIR"):
                ensure_private_dir(env_paths[key])
            ensure_private_dir(os.path.join(rdir, "raw", "rtk-preflight"))
            failures = rtk_smoke_test(info["rtk_bin"], rdir)
            rec["versions"]["rtk"] = info.get("rtk_version")
            rec["rtk"] = {
                "rtk_bin": info.get("rtk_bin"),
                "supported_version": SUPPORTED_RTK_VERSION,
                "preflight": {
                    "external_user_config": "none found",
                    "existing_rtk_hooks": "none found",
                    "project_rtk_dir": info.get("project_rtk_dir"),
                    "smoke_test": "passed" if not failures else "failed",
                    "inherited_rtk_variables_cleared": inherited,
                    "isolation": ("RTK_DB_PATH, RTK_TEE_DIR, RTK_RECALL_DB and RTK_CONFIG_DIR point into this "
                                  "run's private raw/rtk folder; telemetry disabled; no proxy, no user "
                                  "pipelines/delegation; HOME unchanged"),
                },
                "hook": None, "stats": None, "savings_estimate": None,
            }
            if failures:
                rec["state_reason"] = "rtk-preflight-failed"
                atomic_write_json(os.path.join(rdir, "run.json"), rec)
                print_refusals([("rtk smoke test", f) for f in failures])
                return 3
            atomic_write_json(os.path.join(rdir, "run.json"), rec)
            lines.append(("RTK_BIN", info["rtk_bin"]))
            for key in RTK_RUN_ENV_KEYS:
                lines.append((key, env_paths[key]))
    for key, value in lines:
        if "\n" in value or "\t" in value:
            raise ReportError("unsupported character in a run path")
        sys.stdout.write("%s\t%s\n" % (key, value))
    sys.stdout.flush()
    # Automatic, bounded backfill/refresh of the public per-launch RTK
    # publications of FINALIZED runs (stored figures only; runs no RTK;
    # never fails or prints).
    reconcile_quietly()
    return 0


def outcome_from_exit(code):
    if code == 0:
        return "completed", "copilot-exited-0"
    if code in (129, 130, 131, 143):
        return "interrupted", "copilot-exited-on-signal-%d" % (code - 128)
    if code in (126, 127):
        return "copilot-error", "copilot-launch-failed-%d" % code
    return "copilot-error", "copilot-exited-%d" % code


def read_hook_events(rdir):
    path = os.path.join(rdir, "hook-events.jsonl")
    out = {"availability": "none-recorded", "decisions": {d: 0 for d in HOOK_DECISIONS}, "reasons": {},
           "malformed_lines": 0, "session_ids": []}
    try:
        st = os.lstat(path)
    except OSError:
        return out
    if not stat.S_ISREG(st.st_mode) or st.st_size > HOOK_LOG_MAX_BYTES:
        out["availability"] = "unreadable"
        return out
    sids = set()
    with open(path, "rb") as f:
        for raw in f:
            if len(raw) > HOOK_LOG_MAX_LINE:
                out["malformed_lines"] += 1
                continue
            try:
                e = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                out["malformed_lines"] += 1
                continue
            if not isinstance(e, dict) or e.get("decision") not in HOOK_DECISIONS:
                out["malformed_lines"] += 1
                continue
            out["decisions"][e["decision"]] += 1
            reason = e.get("reason")
            if isinstance(reason, str) and HOOK_REASON_RE.match(reason):
                out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
            if valid_session_id(e.get("sid")):
                sids.add(e["sid"])
    out["availability"] = "available"
    out["session_ids"] = sorted(sids)
    return out


def otel_time(pair):
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    a, b = finite_number(pair[0]), finite_number(pair[1])
    if a is None or b is None:
        return None
    return a + b / 1e9


def scan_own_otel(path):
    """Chat spans of this launch's own OTEL file grouped by conversation id."""
    diag = {"file": "missing", "lines": 0, "malformed_lines": 0, "spans": 0, "chat_spans": 0,
            "chat_spans_without_conversation_id": 0}
    by_conv = {}
    if not path:
        diag["file"] = "not-configured"
        return diag, by_conv
    try:
        st = os.lstat(path)
    except OSError:
        return diag, by_conv
    if not stat.S_ISREG(st.st_mode):
        diag["file"] = "unreadable"
        return diag, by_conv
    diag["file"] = "present"
    with open(path, "rb") as f:
        for raw in f:
            diag["lines"] += 1
            if len(raw) > OTEL_MAX_LINE_BYTES:
                diag["malformed_lines"] += 1
                continue
            try:
                e = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                diag["malformed_lines"] += 1
                continue
            if not isinstance(e, dict) or e.get("type") != "span":
                continue
            diag["spans"] += 1
            name = e.get("name")
            if not isinstance(name, str) or not name.startswith("chat"):
                continue
            diag["chat_spans"] += 1
            attrs = e.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            conv = attrs.get("gen_ai.conversation.id")
            if not valid_session_id(conv):
                diag["chat_spans_without_conversation_id"] += 1
                continue
            by_conv.setdefault(conv, []).append(e)
    return diag, by_conv


USAGE_ATTRS = (
    ("prompt_tokens", "gen_ai.usage.input_tokens", "input"),
    ("completion_tokens", "gen_ai.usage.output_tokens", "output"),
    ("reasoning_tokens", "gen_ai.usage.reasoning.output_tokens", "reasoning"),
    ("cache_read_tokens", "gen_ai.usage.cache_read.input_tokens", "cache_read"),
    ("cache_write_tokens", "gen_ai.usage.cache_write.input_tokens", "cache_write"),
)


def call_from_span(e):
    """Model-call dict compatible with copilot-task-report.price_call_official,
    or None when the span carries no usage at all."""
    attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
    if not any(isinstance(k, str) and k.startswith("gen_ai.usage.") for k in attrs):
        return None
    model = safe_label(attrs.get("gen_ai.response.model")) or safe_label(attrs.get("gen_ai.request.model"))
    mc = {"model": model or "unknown-model", "usage_present": {}, "usage_unconvertible": []}
    strict_ok = True
    for key, attr, short in USAGE_ATTRS:
        present = attr in attrs
        mc["usage_present"][short] = present
        value = strict_count(attrs.get(attr)) if present else 0
        if value is None:
            strict_ok = False
            mc["usage_unconvertible"].append(attr)
            value = 0
        mc[key] = value
    mc["usage_strict_ok"] = strict_ok
    start, end = otel_time(e.get("startTime")), otel_time(e.get("endTime"))
    mc["duration_ms"] = int((end - start) * 1000) if start is not None and end is not None and end >= start else None
    return mc


def load_task_report_module():
    """Import copilot-task-report.py — the sibling of this helper's real path,
    else ~/.local/bin — for its official pricing code. COPILOT_TASK_REPORT_HELPER
    is deliberately NOT honored here: importing executes the file, and that
    override may name a wrapper script rather than the real module.
    Bytecode writing is disabled so no __pycache__ appears next to it."""
    candidates = []
    candidates.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "copilot-task-report.py"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".local", "bin", "copilot-task-report.py"))
    for path in candidates:
        if not os.path.isfile(path):
            continue
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("copilot_task_report_for_session_report", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in ("load_official_pricing_context", "official_pricing_snapshot_for_ingest",
                     "price_call_official"):
            if not hasattr(mod, attr):
                raise ReportError("copilot-task-report.py has no %s" % attr)
        return mod
    raise ReportError("copilot-task-report.py not found")


def usage_and_cost(spans):
    """(usage, cost, input_rates) for one session's spans in this launch."""
    seen = set()
    calls, unkeyed, duplicates, no_usage = [], 0, 0, 0
    for e in spans:
        trace, span = e.get("traceId"), e.get("spanId")
        if not (isinstance(trace, str) and OTEL_ID_RE.match(trace) and isinstance(span, str)
                and OTEL_ID_RE.match(span)):
            unkeyed += 1
            continue
        if (trace, span) in seen:
            duplicates += 1
            continue
        seen.add((trace, span))
        mc = call_from_span(e)
        if mc is None:
            # Same rule as the task report: a chat span that never reported
            # usage (e.g. a call that failed early) is not a model call.
            no_usage += 1
        else:
            calls.append(mc)
    usage = {"calls": len(calls), "chat_spans_without_stable_ids": unkeyed, "duplicate_spans_ignored": duplicates,
             "chat_spans_without_usage": no_usage, "invalid_usage_calls": 0,
             "calls_missing_input_or_output": 0, "model_time_ms_sum": 0,
             "calls_without_duration": 0, "by_model": {}, "token_field_coverage": {},
             "token_note": ("token totals sum only values the spans actually reported; a field no call "
                            "reported is null (unknown), and token_field_coverage shows reported vs missing "
                            "calls per field. Pricing follows the task report rule: Copilot omits zero-valued "
                            "cache/reasoning attributes, so an absent one is priced as 0, while a missing "
                            "input or output count leaves the call unpriced."),
             "model_time_note": "sum of model-call span durations; parallel calls overlap",
             "time_to_first_token": "unknown (not captured by copilot-s)"}
    totals = {key: 0 for key, _attr, _short in USAGE_ATTRS}
    reported = {key: 0 for key, _attr, _short in USAGE_ATTRS}
    valid_calls = 0
    for mc in calls:
        row = usage["by_model"].setdefault(mc["model"], {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                         "priced_calls": 0, "estimated_usd": 0.0})
        row["calls"] += 1
        if not mc["usage_strict_ok"]:
            usage["invalid_usage_calls"] += 1
        else:
            valid_calls += 1
            for key, _attr, short in USAGE_ATTRS:
                if mc["usage_present"][short]:
                    reported[key] += 1
                    totals[key] += mc[key]
            if not (mc["usage_present"]["input"] and mc["usage_present"]["output"]):
                usage["calls_missing_input_or_output"] += 1
            row["input_tokens"] += mc["prompt_tokens"]
            row["output_tokens"] += mc["completion_tokens"]
        if mc["duration_ms"] is None:
            usage["calls_without_duration"] += 1
        else:
            usage["model_time_ms_sum"] += mc["duration_ms"]
    names = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens"}
    for key, _attr, _short in USAGE_ATTRS:
        out_key = names.get(key, key)
        usage["token_field_coverage"][out_key] = {"reported_calls": reported[key],
                                                  "missing_calls": valid_calls - reported[key]}
        if out_key in ("input_tokens", "output_tokens"):
            # Required counts: the sum of reported values; a call missing one
            # makes the whole usage `partial` (a disclosed lower bound).
            usage[out_key] = totals[key]
        else:
            usage[out_key] = totals[key] if reported[key] or not valid_calls else None
    if unkeyed or usage["invalid_usage_calls"] or usage["calls_missing_input_or_output"]:
        usage["availability"] = "partial"
    else:
        usage["availability"] = "complete" if calls else "none-observed"

    cost = {"currency": "USD", "basis": NOTE_NOT_A_BILL, "snapshot": None, "priced_calls": 0,
            "unpriced_calls": 0, "unpriced_reasons": {}, "estimated_usd": None}
    input_rates = set()
    try:
        tr = load_task_report_module()
        ctx = tr.load_official_pricing_context()
        snap, no_snap_reason = tr.official_pricing_snapshot_for_ingest(ctx)
    except (Exception, SystemExit) as ex:  # noqa: BLE001 — pricing is optional; report why
        none_observed = usage["availability"] == "none-observed"
        cost["availability"] = "none-observed" if none_observed else "unavailable"
        if none_observed:
            cost["estimated_usd"] = 0.0
        cost["reason"] = "official pricing could not be loaded (%s)" % type(ex).__name__
        cost["unpriced_calls"] = len(calls)
        return usage, cost, input_rates
    if snap is not None:
        cost["snapshot"] = {"id": snap.get("snapshot_id"), "fetched_at": snap.get("fetched_at"),
                            "status": ctx.get("status"), "source": snap.get("public_url"),
                            "unit": snap.get("unit")}
    total = 0.0
    for mc in calls:
        res, why = tr.price_call_official(mc, snap, no_snap_reason)
        row = usage["by_model"][mc["model"]]
        if res is None:
            cost["unpriced_calls"] += 1
            cost["unpriced_reasons"][why] = cost["unpriced_reasons"].get(why, 0) + 1
            continue
        cost["priced_calls"] += 1
        total += res["usd"]
        row["priced_calls"] += 1
        row["estimated_usd"] += res["usd"]
        with contextlib.suppress(KeyError, TypeError):
            rate = snap["models"][res["model_id"]]["tiers"][res["tier"]]["input"]
            if finite_number(rate) is not None:
                input_rates.add(float(rate))
    if cost["priced_calls"]:
        cost["estimated_usd"] = total
    if usage["availability"] == "none-observed":
        # The launch's own OTEL file is present and carries no call for this
        # session: a true zero, not an unknown.
        cost["availability"] = "none-observed"
        cost["estimated_usd"] = 0.0
    elif cost["unpriced_calls"] == 0 and usage["availability"] == "complete":
        cost["availability"] = "complete"
    elif cost["priced_calls"]:
        cost["availability"] = "partial"
    else:
        cost["availability"] = "unavailable"
    return usage, cost, input_rates


NATIVE_USAGE_MAX_DEPTH = 32
NATIVE_USAGE_MAX_INT_DIGITS = 16


def _json_depth_within(text, limit):
    """True if no array/object nests deeper than `limit` (strings skipped)."""
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def _bounded_int(digits):
    # Overlong integers are invalid values (None), never a parse failure.
    return int(digits) if len(digits.lstrip("-")) <= NATIVE_USAGE_MAX_INT_DIGITS else None


def read_native_usage(path):
    """Whitelisted numeric fields of Copilot's --usage-output-file (never the
    modified-file list or anything else)."""
    out = {"availability": "unavailable", "file": path,
           "note": "Copilot's own end-of-session usage summary for this launch; secondary — never summed with "
                   "the OTEL-based figures"}
    data = read_regular_file(path, NATIVE_USAGE_MAX_BYTES) if path else None
    if data is None:
        out["reason"] = "Copilot did not write the usage file (e.g. it was interrupted or failed to start)"
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        out["reason"] = "the usage file is not valid UTF-8"
        return out
    if not _json_depth_within(text, NATIVE_USAGE_MAX_DEPTH):
        out["reason"] = "the usage file nests deeper than %d levels" % NATIVE_USAGE_MAX_DEPTH
        return out
    try:
        # NaN/Infinity and overlong integers become None (invalid values).
        raw = json.loads(text, parse_constant=lambda _c: None, parse_int=_bounded_int)
    except (ValueError, RecursionError, OverflowError, TypeError):
        out["reason"] = "the usage file is not valid JSON"
        return out
    if not isinstance(raw, dict):
        out["reason"] = "unexpected usage file shape"
        return out
    # Whitelisted fields only. A whitelisted field that is present but not a
    # valid value (boolean, negative, fraction, string, NaN/Infinity,
    # overlong integer, wrong type) is listed in invalid_fields and never
    # kept; absent fields are simply unknown. Availability: "available" only
    # if every present whitelisted field is valid, "partial" if some are
    # invalid, "unavailable" if no valid whitelisted field remains.
    invalid = []
    valid = 0

    def take(container, key, dest, dest_key, check, label):
        nonlocal valid
        if key not in container:
            return
        value = check(container[key])
        if value is None:
            invalid.append(label)
            return
        dest[dest_key] = value
        valid += 1

    for key in ("totalNanoAiu", "totalApiDurationMs", "totalUserRequests"):
        take(raw, key, out, key, strict_count, key)
    take(raw, "totalPremiumRequestCost", out, "totalPremiumRequestCost",
         lambda v: finite_number(v) if finite_number(v) is not None and finite_number(v) >= 0 else None,
         "totalPremiumRequestCost")
    take(raw, "aiCreditsStatus", out, "aiCreditsStatus",
         lambda v: v if isinstance(v, str) and re.match(r"^[a-z_-]{1,40}$", v) else None, "aiCreditsStatus")
    models = {}
    if "modelMetrics" in raw:
        mm = raw["modelMetrics"]
        if not isinstance(mm, dict):
            invalid.append("modelMetrics")
        else:
            for name in sorted(mm)[:NATIVE_USAGE_MAX_MODELS]:
                m = mm[name]
                if safe_label(name) is None or not isinstance(m, dict):
                    invalid.append("modelMetrics.<model>")
                    continue
                row = {}
                req = m.get("requests")
                if "requests" in m:
                    if isinstance(req, dict):
                        take(req, "count", row, "requests", strict_count, "modelMetrics.<model>.requests.count")
                    else:
                        invalid.append("modelMetrics.<model>.requests")
                use = m.get("usage")
                if "usage" in m:
                    if isinstance(use, dict):
                        for k in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
                            take(use, k, row, k, strict_count, "modelMetrics.<model>.usage.%s" % k)
                    else:
                        invalid.append("modelMetrics.<model>.usage")
                take(m, "totalNanoAiu", row, "totalNanoAiu", strict_count, "modelMetrics.<model>.totalNanoAiu")
                models[name] = row
    out["models"] = models
    if invalid:
        out["invalid_fields"] = sorted(set(invalid))
    if valid == 0:
        out["availability"] = "unavailable"
        out["reason"] = ("the usage file has no valid whitelisted usage field"
                         + (" (invalid values were discarded)" if invalid else ""))
    else:
        out["availability"] = "partial" if invalid else "available"
    return out


def rtk_gain_summary(rec, rdir):
    """Numeric RTK summary for THIS launch's isolated database only."""
    env_paths = rtk_env_for(rdir)
    out = {"availability": "unavailable", "source": "rtk gain --format json on this run's isolated database",
           "token_note": "RTK's own estimates from command output size, not model tokenizer counts"}
    if not os.path.isfile(env_paths["RTK_DB_PATH"]):
        out["availability"] = "none-recorded"
        out["reason"] = "RTK recorded no command in this launch (its database was never created)"
        return out
    rtk_bin = (rec.get("rtk") or {}).get("rtk_bin")
    if not rtk_bin or not os.path.isfile(rtk_bin):
        out["reason"] = "the RTK binary recorded at launch is missing"
        return out
    try:
        rc, data = run_capture([rtk_bin, "gain", "--format", "json"], sanitized_rtk_env(env_paths),
                               os.path.join(rdir, "raw", "rtk"), RTK_TIMEOUT_SECONDS)
    except ReportError as ex:
        out["reason"] = "rtk gain %s" % ex
        return out
    if rc != 0:
        out["reason"] = "rtk gain exited %d" % rc
        return out
    clean, reason = parse_gain_summary(data)
    if clean is None:
        out["reason"] = reason
        return out
    out.update(clean)
    out["availability"] = "available"
    return out


def parse_gain_summary(data):
    """(numeric summary, None) from `rtk gain --format json` stdout, or
    (None, fixed reason). Only RTK_SUMMARY_INT_KEYS (total_saved signed) and
    a finite avg_savings_pct (else None) are kept."""
    try:
        summary = json.loads(data.decode("utf-8")).get("summary")
    except (ValueError, UnicodeDecodeError, AttributeError, RecursionError):
        summary = None
    if not isinstance(summary, dict):
        return None, "rtk gain returned an unexpected shape"
    clean = {}
    for key in RTK_SUMMARY_INT_KEYS:
        value = strict_signed(summary.get(key)) if key == "total_saved" else strict_count(summary.get(key))
        if value is None:
            return None, "rtk gain field %s is not a valid count" % key
        clean[key] = value
    pct = finite_number(summary.get("avg_savings_pct"))
    clean["avg_savings_pct"] = round(pct, 2) if pct is not None else None
    return clean, None


def rate_coverage(usage, cost):
    """Which of this run's model calls contributed a known input rate."""
    usage = usage if isinstance(usage, dict) else {}
    cost = cost if isinstance(cost, dict) else {}
    calls = usage.get("calls") if isinstance(usage.get("calls"), int) else 0
    priced = cost.get("priced_calls") if isinstance(cost.get("priced_calls"), int) else 0
    unpriced_models = sorted(m for m, row in (usage.get("by_model") or {}).items()
                             if isinstance(row, dict) and row.get("priced_calls", 0) < row.get("calls", 0))
    return {"model_calls": calls, "priced_calls": priced, "unpriced_calls": max(calls - priced, 0),
            "unpriced_models": unpriced_models,
            "usage_availability": usage.get("availability", "unavailable")}


def savings_estimate(stats, input_rates, coverage=None):
    coverage = coverage or {}
    base = {"currency": "USD", "rate_coverage": coverage, "assumptions": [
        "RTK token counts are RTK's own estimates from output size, not model tokenizer counts",
        "each avoided token is valued once as fresh model input (one exposure); cached or repeated "
        "context is not accounted",
        "rate = official input rate(s) of this run's priced model calls; several models give a min..max "
        "range (no per-command model routing is assumed)",
        "estimate only — not a bill, a credit deduction or a measured saving",
    ]}
    if not stats or stats.get("availability") != "available":
        base.update(availability="unknown", reason="no RTK statistics for this launch")
        return base
    saved = stats["total_saved"]
    growth = stats["total_output"] - stats["total_input"]
    base["avoided_tokens_est"] = max(saved, 0)
    if growth > 0:
        base["output_growth_tokens_est"] = growth
    if stats["total_commands"] == 0 or saved <= 0:
        base.update(availability="none", estimated_usd_min=0.0, estimated_usd_max=0.0,
                    reason="RTK recorded no avoided tokens in this launch")
        return base
    if not input_rates:
        base.update(availability="unknown", reason="no priced model call in this launch, so no known input rate")
        return base
    base.update(availability="estimated", input_rate_usd_per_1m_min=min(input_rates),
                input_rate_usd_per_1m_max=max(input_rates),
                estimated_usd_min=saved * min(input_rates) / 1_000_000.0,
                estimated_usd_max=saved * max(input_rates) / 1_000_000.0)
    if coverage.get("unpriced_calls") or coverage.get("usage_availability") != "complete":
        # Some of this run's calls (models) have no known rate: the range
        # covers only the priced models' rates and is NOT a range over all
        # models used — the true value may lie outside it.
        base["availability"] = "partial"
        base["qualification"] = (
            "partial: %d of %d model call(s) have no known official rate (unpriced model(s): %s) or usage "
            "is incomplete; the range uses only the priced models' input rates and may not cover the "
            "unpriced models" % (coverage.get("unpriced_calls", 0), coverage.get("model_calls", 0),
                                 ", ".join(coverage.get("unpriced_models") or []) or "none listed"))
    return base


def copilot_version_from_events(session_dir, launched_epoch):
    path = os.path.join(session_dir, "events.jsonl") if session_dir else None
    if not path or not os.path.isfile(path):
        return None
    found = None
    with open(path, "rb") as f:
        for raw in f:
            if b'"session.start"' not in raw and b'"session.resume"' not in raw:
                continue
            try:
                e = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if not isinstance(e, dict) or e.get("type") not in ("session.start", "session.resume"):
                continue
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            ts = e.get("timestamp") or data.get("startTime")
            try:
                epoch = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError, OSError):
                continue
            if launched_epoch is not None and epoch < launched_epoch - EVENTS_VERSION_SKEW_SECONDS:
                continue
            version = data.get("copilotVersion")
            if isinstance(version, str) and VERSION_RE.match(version):
                found = version
    return found


def resolve_session(rec, launcher_sid, hook_sids, conv_ids, session_state_dir):
    """Exact session link, or unknown with a reason. Never picks 'latest'."""
    kind = rec["launch"]["kind"]
    if kind == "resume":
        cand, basis = rec["launch"]["resume_session_id"], "launcher-selected-resume"
    elif launcher_sid:
        cand, basis = launcher_sid, "unique-new-session-dir"
    else:
        cand, basis = None, None
    evidence = {"hook_session_ids": len(hook_sids), "otel_conversation_ids": len(conv_ids)}
    if cand:
        if hook_sids and cand not in hook_sids:
            return dict(evidence, id=None, status="unknown", basis=None,
                        reason="RTK hook evidence names other session ids only (possible foreign link refused)")
        if conv_ids and cand not in conv_ids:
            return dict(evidence, id=None, status="unknown", basis=None,
                        reason="this launch's OTEL file carries other conversation ids only (link refused)")
        corroborated = [n for n, s in (("rtk-hook", hook_sids), ("otel-conversation", conv_ids)) if cand in s]
        return dict(evidence, id=cand, status="confirmed", basis=basis, corroborated_by=corroborated,
                    other_ids_ignored=len((set(hook_sids) | set(conv_ids)) - {cand}))
    if len(conv_ids) == 1:
        only = next(iter(conv_ids))
        if (session_state_dir and (not hook_sids or only in hook_sids)
                and os.path.isdir(os.path.join(session_state_dir, only))):
            return dict(evidence, id=only, status="confirmed", basis="single-conversation-id-in-own-otel-file",
                        corroborated_by=["rtk-hook"] if only in hook_sids else [], other_ids_ignored=0)
    return dict(evidence, id=None, status="unknown", basis=None,
                reason="no unambiguous session id for this launch (no unique new session directory and "
                       "no single conversation id in the launch's own OTEL file)")


def finalize(rec, rdir, a):
    code = a.exit_code
    outcome, reason = outcome_from_exit(code)
    rec["exit_code"] = code
    rec["copilot_outcome"] = outcome
    rec["state"], rec["state_reason"] = outcome, reason
    launched = finite_number(a.launched_epoch)
    ended = finite_number(a.ended_epoch)
    rec["launched_at"], rec["ended_at"] = iso(launched), iso(ended)
    if launched is not None and ended is not None and ended >= launched:
        rec["wall_clock_ms"] = int(round((ended - launched) * 1000))
        rec["wall_clock_resolution"] = "1s (launcher clock)"

    hook = read_hook_events(rdir) if rec["mode"] == "rtk" else None
    hook_sids = set(hook["session_ids"]) if hook else set()
    diag, by_conv = scan_own_otel(rec["otel"].get("file"))
    launcher_sid = a.session_id if valid_session_id(a.session_id) else None
    session = resolve_session(rec, launcher_sid, hook_sids, set(by_conv), a.session_state_dir)
    rec["session"] = session
    sid = session["id"]
    diag["chat_spans_other_conversations"] = sum(len(v) for k, v in by_conv.items() if k != sid)
    rec["otel"].update(diag)

    input_rates = set()
    if sid is None:
        rec["usage"] = {"availability": "unavailable", "reason": "session id unknown, so no span is attributed"}
        rec["cost"] = {"currency": "USD", "availability": "unavailable", "estimated_usd": None,
                       "reason": "session id unknown", "basis": NOTE_NOT_A_BILL}
    elif diag["file"] != "present":
        rec["usage"] = {"availability": "unavailable", "reason": "this launch's OTEL file is %s" % diag["file"]}
        rec["cost"] = {"currency": "USD", "availability": "unavailable", "estimated_usd": None,
                       "reason": "no OTEL data", "basis": NOTE_NOT_A_BILL}
    else:
        rec["usage"], rec["cost"], input_rates = usage_and_cost(by_conv.get(sid, []))
    if sid is not None and a.session_state_dir:
        rec["versions"]["copilot"] = copilot_version_from_events(os.path.join(a.session_state_dir, sid), launched)
    if rec["versions"]["copilot"] is None:
        rec["versions"]["copilot"] = "unknown"

    if rec["capabilities"]["native_usage_file"] == "owned":
        usage_file = (rec.get("native_usage") or {}).get("file")
        try:
            rec["native_usage"] = read_native_usage(usage_file)
        except Exception as ex:  # noqa: BLE001 — optional source: never fails the run record
            rec["native_usage"] = {"availability": "unavailable", "file": usage_file,
                                   "reason": "the usage file could not be read (%s)" % type(ex).__name__}

    if rec["mode"] == "rtk":
        rtk = rec.get("rtk") or {}
        hook.pop("session_ids", None)
        rtk["hook"] = hook
        rtk["stats"] = rtk_gain_summary(rec, rdir)
        rtk["savings_estimate"] = savings_estimate(rtk["stats"], input_rates,
                                                   rate_coverage(rec.get("usage"), rec.get("cost")))
        rec["rtk"] = rtk


def cmd_finish(a):
    rdir = run_dir(a.run_id)
    rec = load_run(a.run_id)
    with run_lock(rdir):
        rec = load_run(a.run_id)
        if rec["state"] != STATE_INCOMPLETE or rec.get("finalized_at"):
            print("Run %s was already finalized (%s); left unchanged." % (a.run_id, rec["state"]),
                  file=sys.stderr)
            return 0
        try:
            finalize(rec, rdir, a)
        except Exception as ex:  # noqa: BLE001 — never lose the run record
            rec["state"] = "report-failure"
            rec["state_reason"] = "finalization-error:%s" % type(ex).__name__
            if rec.get("copilot_outcome") is None:
                rec["copilot_outcome"] = outcome_from_exit(a.exit_code)[0]
                rec["exit_code"] = a.exit_code
        rec["finalized_at"] = iso(now_epoch())
        atomic_write_json(os.path.join(rdir, "run.json"), rec)
        # RTK mode: publish the stored final figures for viewers while still
        # holding the run lock (a launch's publisher re-checks under this
        # lock, so no active result can replace them). Never affects the
        # canonical record, output or exit status.
        publish_after_finish(rec)
    for line in render_run_text(rec, compact=True):
        print(line)
    return 0 if rec["state"] != "report-failure" else 4


def cmd_mark_outcome(a):
    rdir = run_dir(a.run_id)
    load_run(a.run_id)
    with run_lock(rdir):
        rec = load_run(a.run_id)
        rec["task_outcome"] = {"value": a.outcome, "source": "user-marked" if a.outcome != "unknown" else "user-reset",
                               "marked_at": iso(now_epoch())}
        atomic_write_json(os.path.join(rdir, "run.json"), rec)
    print("Run %s task outcome: %s" % (a.run_id, a.outcome))
    return 0


# --------------------------------------------------------------------------
# Read-only reports
# --------------------------------------------------------------------------

def fmt_usd(value):
    if value is None:
        return "unknown"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return "<$0.01"
    return "~$%.2f" % value


def fmt_usd_range(lo, hi):
    if lo is None or hi is None:
        return "unknown"
    a, b = fmt_usd(lo), fmt_usd(hi)
    return a if a == b else "%s–%s" % (a, b.lstrip("~"))


def fmt_ms(ms):
    if ms is None:
        return "unknown"
    s = int(round(ms / 1000.0))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def render_run_text(rec, compact=False):
    lines = []
    sess = rec.get("session") or {}
    sid = sess.get("id")
    lines.append("Run %s  mode=%s  state=%s  exit=%s" % (rec["run_id"], rec["mode"], rec["state"],
                                                         rec.get("exit_code")))
    lines.append("Session: %s" % ("%s (%s)" % (sid, sess.get("basis")) if sid else
                                  "unknown — %s" % sess.get("reason")))
    lines.append("Wall clock: %s   Task outcome: %s" % (fmt_ms(rec.get("wall_clock_ms")),
                                                        (rec.get("task_outcome") or {}).get("value")))
    usage = rec.get("usage") or {}
    if usage.get("availability") in ("complete", "partial", "none-observed"):
        lines.append("Model calls: %d (%s)  tokens in/out: %d/%d  model time (sum): %s" % (
            usage["calls"], usage["availability"], usage["input_tokens"], usage["output_tokens"],
            fmt_ms(usage.get("model_time_ms_sum"))))
    else:
        lines.append("Model usage: unavailable — %s" % usage.get("reason", "not finalized"))
    cost = rec.get("cost") or {}
    snap = cost.get("snapshot") or {}
    lines.append("Estimated cost: %s (%s%s)" % (
        fmt_usd(cost.get("estimated_usd")), cost.get("availability", "unknown"),
        ", prices %s fetched %s" % (snap.get("id"), snap.get("fetched_at")) if snap else ""))
    if rec["mode"] == "rtk":
        rtk = rec.get("rtk") or {}
        hook = rtk.get("hook") or {}
        dec = hook.get("decisions") or {}
        lines.append("RTK hook: %d rewritten, %d unchanged, %d denied" % (
            dec.get("rewritten", 0), dec.get("unchanged", 0), dec.get("denied", 0)))
        stats = rtk.get("stats") or {}
        if stats.get("availability") == "available":
            lines.append("RTK (estimated tokens): %d commands, %d avoided%s" % (
                stats["total_commands"], max(stats["total_saved"], 0),
                ", output grew by %d" % (stats["total_output"] - stats["total_input"])
                if stats["total_output"] > stats["total_input"] else ""))
        else:
            lines.append("RTK stats: %s" % stats.get("reason", stats.get("availability", "unknown")))
        sav = rtk.get("savings_estimate") or {}
        if sav.get("availability") in ("estimated", "partial"):
            lines.append("Approx. value of avoided tokens (one exposure, list input price): %s%s" % (
                fmt_usd_range(sav.get("estimated_usd_min"), sav.get("estimated_usd_max")),
                " — %s" % sav.get("qualification") if sav.get("availability") == "partial" else ""))
        elif sav:
            lines.append("Approx. value of avoided tokens: %s" % (
                "$0.00" if sav.get("availability") == "none" else "unknown — %s" % sav.get("reason")))
    if not compact:
        lines.append("Versions: copilot-s %s, copilot %s%s" % (
            (rec.get("versions") or {}).get("copilot_s"), (rec.get("versions") or {}).get("copilot"),
            ", rtk %s" % rec["versions"]["rtk"] if (rec.get("versions") or {}).get("rtk") else ""))
        for note in rec.get("baseline_notes") or []:
            lines.append("Note: %s" % note)
        raw = (rec.get("privacy") or {}).get("private_raw_store") or {}
        lines.append("Private raw store (not part of this report, never auto-deleted): %s" % raw.get("path"))
        lines.append(NOTE_NOT_A_BILL)
    else:
        lines.append("Report: copilot-session-report.py launch --run-id %s" % rec["run_id"])
    return lines


def _blank_group():
    return {"runs": 0, "launched_runs": 0, "states": {}, "task_outcomes": {}, "wall_clock_ms_sum": 0,
            "wall_clock_runs": 0, "model_time_ms_sum": 0, "usage_runs": 0, "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "estimated_usd_sum": 0.0, "cost_runs_complete": 0, "cost_runs_partial": 0,
            "cost_runs_unknown": 0, "rtk_commands": 0, "rtk_avoided_tokens_est": 0,
            "rtk_value_usd_min_sum": 0.0, "rtk_value_usd_max_sum": 0.0, "rtk_value_runs_unknown": 0,
            "rtk_value_runs_partial": 0}


def _add_to_group(g, rec):
    g["runs"] += 1
    g["states"][rec["state"]] = g["states"].get(rec["state"], 0) + 1
    if rec.get("launched_at"):
        g["launched_runs"] += 1
    out = (rec.get("task_outcome") or {}).get("value", "unknown")
    g["task_outcomes"][out] = g["task_outcomes"].get(out, 0) + 1
    if isinstance(rec.get("wall_clock_ms"), int):
        g["wall_clock_ms_sum"] += rec["wall_clock_ms"]
        g["wall_clock_runs"] += 1
    usage = rec.get("usage") or {}
    if usage.get("availability") in ("complete", "partial", "none-observed"):
        g["usage_runs"] += 1
        g["calls"] += usage.get("calls", 0)
        g["input_tokens"] += usage.get("input_tokens", 0)
        g["output_tokens"] += usage.get("output_tokens", 0)
        g["model_time_ms_sum"] += usage.get("model_time_ms_sum", 0)
    cost = rec.get("cost") or {}
    if cost.get("availability") in ("complete", "none-observed"):
        g["cost_runs_complete"] += 1
    elif cost.get("availability") == "partial":
        g["cost_runs_partial"] += 1
    else:
        g["cost_runs_unknown"] += 1
    if isinstance(cost.get("estimated_usd"), (int, float)):
        g["estimated_usd_sum"] += cost["estimated_usd"]
    if rec["mode"] == "rtk":
        rtk = rec.get("rtk") or {}
        stats = rtk.get("stats") or {}
        if stats.get("availability") == "available":
            g["rtk_commands"] += stats["total_commands"]
            g["rtk_avoided_tokens_est"] += max(stats["total_saved"], 0)
        sav = rtk.get("savings_estimate") or {}
        if sav.get("availability") in ("estimated", "partial", "none"):
            g["rtk_value_usd_min_sum"] += sav.get("estimated_usd_min", 0.0)
            g["rtk_value_usd_max_sum"] += sav.get("estimated_usd_max", 0.0)
            if sav.get("availability") == "partial":
                g["rtk_value_runs_partial"] += 1
        else:
            g["rtk_value_runs_unknown"] += 1


def group_runs(records):
    groups = {}
    for rec in records:
        _add_to_group(groups.setdefault(rec["mode"], _blank_group()), rec)
    return {m: groups[m] for m in MODES if m in groups}


def render_groups_text(groups):
    lines = []
    for mode, g in groups.items():
        lines.append("[%s] runs: %d (launched %d)  states: %s  task outcomes: %s" % (
            mode, g["runs"], g["launched_runs"], json.dumps(g["states"], sort_keys=True),
            json.dumps(g["task_outcomes"], sort_keys=True)))
        lines.append("  wall clock: %s over %d run(s)   model time (sum): %s" % (
            fmt_ms(g["wall_clock_ms_sum"]), g["wall_clock_runs"], fmt_ms(g["model_time_ms_sum"])))
        lines.append("  model calls: %d  tokens in/out: %d/%d  (usage known for %d run(s))" % (
            g["calls"], g["input_tokens"], g["output_tokens"], g["usage_runs"]))
        partial = g["cost_runs_partial"] + g["cost_runs_unknown"]
        lines.append("  estimated cost: %s%s" % (
            fmt_usd(g["estimated_usd_sum"]) if g["cost_runs_complete"] + g["cost_runs_partial"] else "unknown",
            " (partial: %d run(s) incomplete or unknown)" % partial if partial else ""))
        if mode == "rtk":
            lines.append("  RTK: %d command(s), ~%d avoided tokens (estimated); value %s%s" % (
                g["rtk_commands"], g["rtk_avoided_tokens_est"],
                fmt_usd_range(g["rtk_value_usd_min_sum"], g["rtk_value_usd_max_sum"]),
                " (unknown for %d run(s))" % g["rtk_value_runs_unknown"] if g["rtk_value_runs_unknown"] else "")
                + (" (partial — priced models only — for %d run(s))" % g["rtk_value_runs_partial"]
                   if g["rtk_value_runs_partial"] else ""))
    return lines


def emit(fmt, payload, text_lines):
    if fmt == "json":
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        for line in text_lines:
            print(line)


def cmd_launch(a):
    rec = load_run(a.run_id)
    emit(a.format, rec, render_run_text(rec))
    return 0


def cmd_session(a):
    if not valid_session_id(a.session_id):
        raise ReportError("invalid session id")
    records, skipped = iter_runs()
    mine = [r for r in records if (r.get("session") or {}).get("id") == a.session_id]
    groups = group_runs(mine)
    payload = {"schema": "copilot-s.session-report", "schema_version": 1, "session_id": a.session_id,
               "run_ids": [r["run_id"] for r in mine], "modes_present": list(groups), "mixed_modes": len(groups) > 1,
               "by_mode": groups, "unreadable_run_records": skipped,
               "notes": [NOTE_COVERAGE, NOTE_NOT_A_BILL] + ([NOTE_OBSERVATIONAL] if len(groups) > 1 else [])}
    lines = ["Session %s — %d copilot-s launch(es)%s" % (a.session_id, len(mine),
                                                          " (mixed normal/RTK)" if len(groups) > 1 else "")]
    if not mine:
        lines.append("No session-report run is linked to this session id.")
    for rec in mine:
        lines.append("  %s  %s  %s  %s" % (rec["run_id"], rec["mode"], rec.get("launched_at") or "-", rec["state"]))
    lines += render_groups_text(groups)
    lines += ["Note: " + n for n in payload["notes"]]
    emit(a.format, payload, lines)
    return 0 if mine else 1


def cmd_compare(a):
    records, skipped = iter_runs()
    groups = group_runs(records)
    unlinked = sum(1 for r in records if not (r.get("session") or {}).get("id"))
    payload = {"schema": "copilot-s.session-compare", "schema_version": 1, "by_mode": groups,
               "runs_without_session_link": unlinked, "unreadable_run_records": skipped,
               "notes": [NOTE_OBSERVATIONAL, NOTE_COVERAGE, NOTE_NOT_A_BILL]}
    lines = ["copilot-s launches by mode (%d run record(s))" % len(records)]
    lines += render_groups_text(groups)
    if unlinked:
        lines.append("Runs without a confirmed session link: %d" % unlinked)
    lines += ["Note: " + n for n in payload["notes"]]
    emit(a.format, payload, lines)
    return 0


# --------------------------------------------------------------------------
# Automatic RTK publication: public, numeric-only per-launch files for
# optional external viewers (e.g. the Agentic Watcher macOS app)
# --------------------------------------------------------------------------
#
# One file per RTK-mode launch, written automatically (there is no manual
# export step):
#
#   ~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json
#   (schema `copilot-s.rtk-launch` v1; folders 0700, files 0600, atomic)
#
# - While a `copilot-s --rtk` launch runs, its own launch-scoped publisher
#   (`rtk-publisher`, started by copilot-s right before Copilot and stopped
#   AND reaped by it right after Copilot exits, before `finish`; never a
#   daemon) runs one bounded `rtk gain --format json` on the launch's
#   private database about every PUBLISHER_INTERVAL_SECONDS and republishes
#   the file. One process per launch, strictly sequential: RTK calls never
#   overlap. Any failure leaves the previous file untouched (its
#   `published_at` is not refreshed, so viewers see it age).
# - `finish` publishes the statistics it stored at finalization (RTK is not
#   run again), replacing the active file, while it still holds the run
#   lock; a publisher re-reads the record under that same lock before every
#   write and never overwrites a finalized run.
# - `start` (every launch with a report, any mode) runs one bounded,
#   resumable reconciliation slice: finalized RTK-mode runs are
#   (re)published from their stored records (RTK is never run for it) and
#   publications no longer backed by a publishable record, or not rewritten
#   for PUBLICATION_RETENTION_SECONDS, are withdrawn.
#
# Each launch has its own file (parallel, resumed and multi-launch sessions
# never overwrite each other) and files are never combined or summed. They
# hold ids, states, timestamps, proof labels and numbers only — never
# commands, prompts, output, paths, error text or cost/billing data. Run
# records, reports, history and accounting are never modified.

PUBLICATION_SCHEMA = "copilot-s.rtk-launch"
PUBLICATION_SCHEMA_VERSION = 1
EXPORT_DIR_NAME = "copilot-s-rtk-export"
PUBLICATION_VERSION_DIR = "v1"
PUBLICATION_RUNS_DIR = "runs"
PUBLICATION_SUFFIX = ".json"
PUBLICATION_MAX_BYTES = 16 * 1024
# New files are refused at this many publications. A live (publisher) or
# `finish` write that needs a NEW file at the cap first evicts the oldest
# FINALIZED publication(s) — oldest `finalized_at` first; only files that
# pass the full publication contract are candidates, active ones never are
# (if every file is active the write fails closed). Reconciliation backfill
# never evicts and stops adding at BACKFILL_MAX_FILES. Every write (count,
# eviction, replace) is serialized by PUBLICATION_CAPACITY_LOCK_NAME, so
# concurrent writers cannot overrun the cap. Eviction removes only this
# sanitized cache file; run records and reports are never touched.
PUBLICATION_MAX_FILES = 256
BACKFILL_MAX_FILES = 128
PUBLICATION_CAPACITY_LOCK_NAME = ".capacity.lock"
PUBLICATION_CAPACITY_LOCK_WAIT_SECONDS = 5
# Publications not rewritten for this long are withdrawn by
# reconciliation; finalized runs older than this are not backfilled.
PUBLICATION_RETENTION_SECONDS = 14 * 24 * 3600
PUBLICATION_TMP_PREFIX = ".tmp-"
PUBLICATION_TMP_MAX_AGE_SECONDS = 3600
PUBLISHER_INTERVAL_SECONDS = 30
PUBLISHER_FIRST_DELAY_SECONDS = 2
PUBLISHER_LOCK_WAIT_SECONDS = 2
# One reconciliation slice (per `start`): at most this many run folders
# probed (metadata only), records loaded, and wall-clock seconds.
RECONCILE_SCAN_BUDGET = 256
RECONCILE_LOAD_BUDGET = 32
RECONCILE_TIME_BUDGET_SECONDS = 1.0
RECONCILE_MAX_NAMES = 100000
RECONCILE_CURSOR_NAME = "reconcile-cursor"
RECONCILE_LOCK_NAME = ".reconcile.lock"
EXPORT_PRODUCER = "copilot-session-report.py"
EXPORT_RECORD_STATES = ("active-unfinalized", "finalized")
PUBLICATION_MEASUREMENTS = {"active-unfinalized": "measured-while-active",
                            "finalized": "stored-at-finalization"}
EXPORT_SESSION_PROOFS = ("launcher-selected-resume", "single-conversation-id-in-own-otel-file")
EXPORT_CORROBORATIONS = ("rtk-hook", "otel-conversation")
# Upper bound for each own-run evidence file read for a publication link
# (the hook log of the run and the launch's own OTEL file). Larger = unusable.
EXPORT_EVIDENCE_MAX_BYTES = 64 * 1024 * 1024
EXPORT_RTK_AVAILABILITY = ("available", "none-recorded", "unavailable")
ISO_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
PUBLICATION_NAME_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$")


def publication_paths():
    """(~/.copilot, export folder, version folder, runs folder) — fixed, not
    configurable (COPILOT_SESSION_REPORTS_DIR does not move them)."""
    copilot = os.path.join(os.path.expanduser("~"), ".copilot")
    export = os.path.join(copilot, EXPORT_DIR_NAME)
    version = os.path.join(export, PUBLICATION_VERSION_DIR)
    return copilot, export, version, os.path.join(version, PUBLICATION_RUNS_DIR)


def _owned_plain_dir(path, create):
    """lstat-validated directory owned by us and not group/world-writable
    (symlinks refused, never followed). `create` makes it 0700 if missing.
    An existing unsafe directory is refused, never repaired (no chmod)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise ReportError("%s does not exist" % path)
        os.mkdir(path, 0o700)
        st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ReportError("%s is not a plain directory (symlinks are refused)" % path)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ReportError("%s is not owned by the current user" % path)
    if st.st_mode & 0o022:
        raise ReportError("%s is writable by group or others; refused (not repaired)" % path)
    return st


def ensure_publication_dirs():
    """(version folder, runs folder), creating missing ones 0700."""
    paths = publication_paths()
    for path in paths:
        _owned_plain_dir(path, create=True)
    return paths[2], paths[3]


def existing_publication_dirs():
    """(version folder, runs folder) when the whole chain exists and is
    safe; None when any folder is missing; ReportError when one is unsafe."""
    paths = publication_paths()
    for path in paths:
        if not os.path.lexists(path):
            return None
        _owned_plain_dir(path, create=False)
    return paths[2], paths[3]


def _publication_run_id(name):
    m = PUBLICATION_NAME_RE.match(name or "")
    return m.group(1) if m else None


def _iso_or_none(value):
    return value if isinstance(value, str) and ISO_UTC_RE.match(value) else None


def _epoch_of_iso(value):
    if not _iso_or_none(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _unknown_session():
    return {"link": "unknown", "id": None, "proof": None, "corroborated_by": []}


def _export_reports_chain():
    """(anchor, components) reaching the reports root for export-side
    no-follow opens: the configured COPILOT_SESSION_REPORTS_DIR as given,
    else the account home + .copilot/session-reports (no symlink below
    home is followed)."""
    override = os.environ.get("COPILOT_SESSION_REPORTS_DIR")
    if override:
        return os.path.abspath(override), []
    return os.path.expanduser("~"), [".copilot", "session-reports"]


def _check_private_dir_fd(fd):
    """Directory descriptor owned by us and not group/world-writable."""
    st = os.fstat(fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022
            or (hasattr(os, "getuid") and st.st_uid != os.getuid())):
        raise PermissionError("unsafe directory")


_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_dir_nofollow(base, names):
    """Directory fd of base/names... (the caller closes it). `base` itself is
    opened as given (the account home or the configured reports root);
    every component below it is opened relative to its parent's descriptor
    with O_NOFOLLOW, so a symlink anywhere below `base` is refused
    (ELOOP/ENOTDIR), never followed, and each directory below `base` must be
    owned by us and not group/world-writable (checked on its open
    descriptor; PermissionError otherwise)."""
    fd = os.open(base, _DIR_OPEN_FLAGS)
    try:
        for name in names:
            nfd = os.open(name, _DIR_OPEN_FLAGS | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
            _check_private_dir_fd(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_evidence_nofollow(base, names, final):
    """Read-only fd of base/names.../final through _open_dir_nofollow, the
    file itself opened with O_NOFOLLOW (O_NONBLOCK keeps a FIFO from
    blocking the open)."""
    fd = _open_dir_nofollow(base, names)
    try:
        return os.open(final, os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                       | getattr(os, "O_CLOEXEC", 0), dir_fd=fd)
    finally:
        os.close(fd)


RUN_RECORD_MAX_BYTES = 8 * 1024 * 1024


def load_run_for_export(run_id):
    """Export-specific run-record read (the canonical load_run is unchanged):
    reports root → runs → <run-id> → run.json opened without following any
    symlink below the root's anchor, every directory owned by us and not
    group/world-writable, and run.json a regular single-link file owned by
    us, not group/world-writable and at most 8 MiB — all checked on the open
    descriptors. Raises ReportError (fixed text) otherwise."""
    if not RUN_ID_RE.match(run_id or ""):
        raise ReportError("invalid run id")
    base, names = _export_reports_chain()
    try:
        fd = _open_evidence_nofollow(base, names + ["runs", run_id], "run.json")
    except FileNotFoundError:
        raise ReportError("run record %s not found" % run_id)
    except OSError:
        raise ReportError("run record %s is not reachable through plain, private directories (symlinks and "
                          "group/world-writable folders are refused)" % run_id)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022
                or st.st_size > RUN_RECORD_MAX_BYTES
                or (hasattr(os, "getuid") and st.st_uid != os.getuid())):
            raise ReportError("run record %s is not a private regular file" % run_id)
        data = f.read(RUN_RECORD_MAX_BYTES + 1)
    if len(data) > RUN_RECORD_MAX_BYTES:
        raise ReportError("run record %s is too large" % run_id)
    try:
        rec = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ReportError("run record %s is unreadable" % run_id)
    if (not isinstance(rec, dict) or rec.get("schema") != RUN_SCHEMA
            or rec.get("schema_version") != RUN_SCHEMA_VERSION or rec.get("run_id") != run_id):
        raise ReportError("run record %s has an unsupported schema" % run_id)
    return rec


def _private_evidence_stat_ok(st):
    """Regular single-link file owned by us, not group/world-writable and
    within EXPORT_EVIDENCE_MAX_BYTES (checked on an open descriptor)."""
    return (stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_size <= EXPORT_EVIDENCE_MAX_BYTES
            and not st.st_mode & 0o022
            and (not hasattr(os, "getuid") or st.st_uid == os.getuid()))


def _scan_own_evidence(base, names, final, max_line, consume):
    """Secure bounded reader for one complete own-run evidence file.
    Returns "missing" (no such file: no evidence), True (every record read
    and accepted by `consume`) or False (unusable: symlink, not a regular
    single-link file owned by us and not group/world-writable (never
    repaired), larger than EXPORT_EVIDENCE_MAX_BYTES,
    unreadable, an overlong or unterminated (truncated) line, or a record
    `consume` rejects as malformed/ambiguous). Identity checks run on the
    opened descriptor, so nothing is checked before a separate open."""
    try:
        fd = _open_evidence_nofollow(base, names, final)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return False
    with os.fdopen(fd, "rb") as f:
        try:
            if not _private_evidence_stat_ok(os.fstat(f.fileno())):
                return False
            total = 0
            while True:
                line = f.readline(max_line + 1)
                if not line:
                    return True
                total += len(line)
                if total > EXPORT_EVIDENCE_MAX_BYTES or not line.endswith(b"\n"):
                    return False
                body = line[:-1]
                if not body.strip():
                    continue
                if not consume(body):
                    return False
        except OSError:
            return False


def _json_record(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _hook_line_session_id(body):
    """(ok, session id) of one record of the run's own hook log; ok is False
    for a malformed record or one without a valid session id."""
    e = _json_record(body)
    if e is None or e.get("decision") not in HOOK_DECISIONS or not valid_session_id(e.get("sid")):
        return False, None
    return True, e["sid"]


def _otel_line_conversation_id(body):
    """(ok, conversation id or None) of one line of the launch's own OTEL
    file: a `chat` span must carry a valid conversation id (else ok is
    False); metrics/logs/other spans carry no conversation evidence."""
    e = _json_record(body)
    if e is None:
        return False, None
    if e.get("type") != "span":
        return True, None
    name = e.get("name")
    if not isinstance(name, str) or not name.startswith("chat"):
        return True, None
    attrs = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
    conv = attrs.get("gen_ai.conversation.id")
    if not valid_session_id(conv):
        return False, None
    return True, conv


def _hook_log_location(run_id):
    base, names = _export_reports_chain()
    return base, names + ["runs", run_id], "hook-events.jsonl"


def _own_hook_session_ids(rec):
    """Session ids the run's own RTK hook logged (complete-file scan), or
    None if the log is unusable or any record is malformed or carries no
    valid session id."""
    sids = set()

    def consume(body):
        ok, sid = _hook_line_session_id(body)
        if ok:
            sids.add(sid)
        return ok

    base, names, final = _hook_log_location(rec["run_id"])
    if _scan_own_evidence(base, names, final, HOOK_LOG_MAX_LINE, consume) is False:
        return None
    return sids


def _own_otel_location(rec):
    """Where the launch's own OTEL file is, from a NOT-yet-finalized record
    (finish replaces the recorded path with a status word):
    ("none", None) when no OTEL file is recorded (no evidence);
    ("invalid", None) when the recorded path is not exactly the launcher's
    per-run file (~/.copilot/otel/copilot-otel-<YYYYMMDD-HHMMSS>-<run-id>.jsonl);
    else ("ok", (home, [".copilot", "otel"], file name))."""
    otel = rec.get("otel") if isinstance(rec.get("otel"), dict) else {}
    path = otel.get("file")
    if not path:
        return "none", None
    home = os.path.expanduser("~")
    expected_dir = os.path.join(home, ".copilot", "otel")
    name_re = re.compile(r"^copilot-otel-[0-9]{8}-[0-9]{6}-%s\.jsonl$" % re.escape(rec["run_id"]))
    if (not isinstance(path, str) or os.path.dirname(path) != expected_dir
            or not name_re.match(os.path.basename(path))):
        return "invalid", None
    return "ok", (home, [".copilot", "otel"], os.path.basename(path))


class _EvidenceTail:
    """Publisher-only incremental reader of one append-only own-run evidence
    file (the run's hook log or the launch's own OTEL file) while the launch
    runs. Every poll reopens it through _open_evidence_nofollow and applies
    the same descriptor checks as _scan_own_evidence (regular, single link,
    owned by us, not group/world-writable, at most EXPORT_EVIDENCE_MAX_BYTES
    in total) and the same per-record parser, reading only what was
    appended since the last poll. Only complete (newline-terminated) lines
    are consumed: the unterminated tail of a file still being written is left
    for the next poll (a tail longer than the line limit is unusable).
    Once unusable — unsafe or oversized file, unreadable, a malformed or
    overlong record, or the file replaced, truncated or removed after it was
    read — it stays unusable for the rest of the launch (never read as "no
    conflict"). A file that does not exist yet is no evidence (empty set)."""

    def __init__(self, location, max_line, parse):
        self.base, self.names, self.final = location
        self.max_line = max_line
        self.parse = parse
        self.identity = None
        self.offset = 0
        self.ids = set()
        self.unusable = False

    def _fail(self):
        self.unusable = True
        self.ids = set()
        return None

    def poll(self):
        """Ids seen so far (a new set), or None when unusable."""
        if self.unusable:
            return None
        try:
            fd = _open_evidence_nofollow(self.base, self.names, self.final)
        except FileNotFoundError:
            return set() if self.identity is None else self._fail()
        except OSError:
            return self._fail()
        with os.fdopen(fd, "rb") as f:
            try:
                st = os.fstat(f.fileno())
                if not _private_evidence_stat_ok(st):
                    return self._fail()
                identity = (st.st_dev, st.st_ino)
                if self.identity is not None and (identity != self.identity or st.st_size < self.offset):
                    return self._fail()
                self.identity = identity
                f.seek(self.offset)
                while self.offset < st.st_size:
                    line = f.readline(self.max_line + 1)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        if len(line) > self.max_line:
                            return self._fail()
                        break  # still being written: read once complete
                    if self.offset + len(line) > EXPORT_EVIDENCE_MAX_BYTES:
                        return self._fail()
                    self.offset += len(line)
                    body = line[:-1]
                    if not body.strip():
                        continue
                    ok, value = self.parse(body)
                    if not ok:
                        return self._fail()
                    if value is not None:
                        self.ids.add(value)
            except OSError:
                return self._fail()
        return set(self.ids)


def _link_from_evidence(rec, hook, conv):
    """Publication session link of a launch that is NOT finalized yet, from
    its own explicit evidence only (`hook`/`conv`: id sets, or None when
    unusable) — independent of (and never widening) the canonical report
    link. The only proofs are:

    - launcher-selected-resume: the launcher recorded a resume of exactly
      this session id, and no session id in the run's own hook log and no
      conversation id in its own OTEL file names another session;
    - single-conversation-id-in-own-otel-file: a new launch whose own OTEL
      file names exactly one conversation id (every chat span carries one),
      and every hook session id is that id.

    Finalized runs use _finalized_session_link instead. Anything else —
    unusable evidence, conflicting or several ids, directory discovery
    (`unique-new-session-dir`), times, cwd or "latest" — is unknown."""
    launch = rec.get("launch") if isinstance(rec.get("launch"), dict) else {}
    if hook is None or conv is None:
        return _unknown_session()
    kind = launch.get("kind")
    if kind == "resume":
        cand, proof = launch.get("resume_session_id"), "launcher-selected-resume"
        if not valid_session_id(cand) or (hook - {cand}) or (conv - {cand}):
            return _unknown_session()
    elif kind == "new" and len(conv) == 1:
        cand, proof = next(iter(conv)), "single-conversation-id-in-own-otel-file"
        if hook - {cand}:
            return _unknown_session()
    else:
        return _unknown_session()
    corroborated = []
    if cand in hook:
        corroborated.append("rtk-hook")
    if proof == "launcher-selected-resume" and cand in conv:
        corroborated.append("otel-conversation")
    return {"link": "confirmed", "id": cand, "proof": proof, "corroborated_by": corroborated}


class _PublisherEvidence:
    """The live launch's own evidence, read incrementally across publisher
    cycles (memory only; nothing is persisted)."""

    def __init__(self, rec):
        self.hook = _EvidenceTail(_hook_log_location(rec["run_id"]), HOOK_LOG_MAX_LINE, _hook_line_session_id)
        status, location = _own_otel_location(rec)
        self.otel_invalid = status == "invalid"
        self.otel = (_EvidenceTail(location, OTEL_MAX_LINE_BYTES, _otel_line_conversation_id)
                     if status == "ok" else None)

    def session_link(self, rec):
        hook = self.hook.poll()
        if self.otel_invalid:
            conv = None
        else:
            conv = self.otel.poll() if self.otel is not None else set()
        return _link_from_evidence(rec, hook, conv)


FINALIZED_OTEL_COUNTERS = ("malformed_lines", "chat_spans", "chat_spans_without_conversation_id",
                           "chat_spans_other_conversations")


def _finalized_session_link(rec):
    """Publication link of a finalized run from what finalization stored (no
    canonical field is changed or re-derived; the original OTEL file is not
    re-read because its path is no longer stored), failing closed:

    - rec["session"]: status `confirmed`, a valid id, basis exactly one of
      EXPORT_SESSION_PROOFS (a stored `unique-new-session-dir` or any other
      basis is unknown), strict integer counters `hook_session_ids` and
      `otel_conversation_ids` (each 0 or 1) and `other_ids_ignored` == 0, and
      `corroborated_by` a list naming the evidence those counters imply;
    - rec["otel"] diagnostics: `file` `present` with strict integer
      counters, no malformed line, no chat span without a conversation id or
      of another conversation, and exactly one conversation id iff chat spans
      exist; or `missing`/`not-configured` with no conversation id (allowed
      for an explicit resume only); `unreadable` or anything else is unknown;
    - launcher-selected-resume also needs launch kind `resume` with the same
      recorded resume id; single-conversation-id-in-own-otel-file needs a new
      launch with a present OTEL file and exactly one conversation id;
    - rec["rtk"]["hook"]: availability `available`/`none-recorded` and
      `malformed_lines` == 0, and a fresh secure scan of the run's hook log
      must name no other session and exactly as many ids as stored.

    Missing, boolean or invalid counters are never read as 0."""
    unknown = _unknown_session()
    session = rec.get("session") if isinstance(rec.get("session"), dict) else {}
    launch = rec.get("launch") if isinstance(rec.get("launch"), dict) else {}
    otel = rec.get("otel") if isinstance(rec.get("otel"), dict) else {}
    rtk = rec.get("rtk") if isinstance(rec.get("rtk"), dict) else {}
    hook_stored = rtk.get("hook") if isinstance(rtk.get("hook"), dict) else {}

    cand, proof = session.get("id"), session.get("basis")
    stored_corr = session.get("corroborated_by")
    if (session.get("status") != "confirmed" or not valid_session_id(cand) or proof not in EXPORT_SESSION_PROOFS
            or not isinstance(stored_corr, list)):
        return unknown
    hook_count = strict_count(session.get("hook_session_ids"))
    conv_count = strict_count(session.get("otel_conversation_ids"))
    others = strict_count(session.get("other_ids_ignored"))
    if hook_count is None or conv_count is None or others != 0 or hook_count > 1 or conv_count > 1:
        return unknown

    status = otel.get("file")
    if status == "present":
        counters = [strict_count(otel.get(k)) for k in FINALIZED_OTEL_COUNTERS]
        if any(c is None for c in counters):
            return unknown
        malformed, chat, without_id, other_conv = counters
        if malformed or without_id or other_conv or conv_count != (1 if chat else 0):
            return unknown
    elif status in ("missing", "not-configured"):
        if conv_count != 0:
            return unknown
    else:
        return unknown
    if conv_count == 1 and proof == "launcher-selected-resume" and "otel-conversation" not in stored_corr:
        return unknown

    if proof == "launcher-selected-resume":
        if launch.get("kind") != "resume" or launch.get("resume_session_id") != cand:
            return unknown
    elif launch.get("kind") != "new" or status != "present" or conv_count != 1:
        return unknown

    if (hook_stored.get("availability") not in ("available", "none-recorded")
            or strict_count(hook_stored.get("malformed_lines")) != 0):
        return unknown
    if hook_count == 1 and "rtk-hook" not in stored_corr:
        return unknown
    fresh = _own_hook_session_ids(rec)
    if fresh is None or (fresh - {cand}) or len(fresh) != hook_count:
        return unknown

    corroborated = []
    if fresh:
        corroborated.append("rtk-hook")
    if proof == "launcher-selected-resume" and conv_count == 1:
        corroborated.append("otel-conversation")
    return {"link": "confirmed", "id": cand, "proof": proof, "corroborated_by": corroborated}


def _export_stats(stats):
    """(availability, numeric stats or None) re-validated from a stored
    rtk_gain_summary result; anything invalid is `unavailable`, never 0."""
    if not isinstance(stats, dict):
        return "unavailable", None
    availability = stats.get("availability")
    if availability == "none-recorded":
        return "none-recorded", None
    if availability != "available":
        return "unavailable", None
    clean = {}
    for key in RTK_SUMMARY_INT_KEYS:
        value = strict_signed(stats.get(key)) if key == "total_saved" else strict_count(stats.get(key))
        if value is None:
            return "unavailable", None
        clean[key] = value
    pct = stats.get("avg_savings_pct")
    if pct is None:
        clean["avg_savings_pct"] = None
    else:
        pct = finite_number(pct)
        if pct is None:
            return "unavailable", None
        clean["avg_savings_pct"] = round(pct, 2)
    return "available", clean


def _export_rtk(availability, stats, measured_at, measurement):
    return {"availability": availability, "measurement": measurement,
            "measured_at": measured_at if availability != "unavailable" else None,
            "stats": stats if availability == "available" else None}


def _is_finalized(rec):
    return rec.get("state") != STATE_INCOMPLETE or bool(rec.get("finalized_at"))


def _publishable_rtk_record(rec):
    """An RTK-mode run whose preflight passed (Copilot was launched in RTK
    mode). Normal-mode and never-launched runs are never published."""
    if rec.get("mode") != "rtk" or rec.get("state_reason") == "rtk-preflight-failed":
        return False
    rtk = rec.get("rtk") if isinstance(rec.get("rtk"), dict) else {}
    preflight = rtk.get("preflight") if isinstance(rtk.get("preflight"), dict) else {}
    return preflight.get("smoke_test") == "passed"


def _publication_entry_base(rec, record_state):
    launch = rec.get("launch") if isinstance(rec.get("launch"), dict) else {}
    return {"run_id": rec["run_id"], "mode": "rtk", "record_state": record_state,
            "launch_kind": launch.get("kind") if launch.get("kind") in LAUNCH_KINDS else None,
            "created_at": _iso_or_none(rec.get("created_at")),
            "finalized_at": _iso_or_none(rec.get("finalized_at")) if record_state == "finalized" else None}


def finalized_publication_run(rec):
    """Finalized run: stored statistics (RTK is not run) and the link from
    _finalized_session_link."""
    entry = _publication_entry_base(rec, "finalized")
    entry["session"] = _finalized_session_link(rec)
    rtk = rec.get("rtk") if isinstance(rec.get("rtk"), dict) else {}
    availability, stats = _export_stats(rtk.get("stats"))
    # The statistics were read during finalization (just before
    # finalized_at); there is no separate measurement timestamp.
    entry["rtk"] = _export_rtk(availability, stats, entry["finalized_at"], PUBLICATION_MEASUREMENTS["finalized"])
    return entry


def _validated_rtk_binary(rec):
    """The RTK binary recorded at launch by `start` (already a realpath),
    re-validated without running it: absolute, no symlink component, a
    regular executable owned by us or root, not group/world-writable, no
    setuid/setgid, in a directory owned by us or root that is not
    group/world-writable, and the record must name the supported RTK
    version. Fails closed (None) — no PATH lookup, no fallback."""
    rtk = rec.get("rtk") if isinstance(rec.get("rtk"), dict) else {}
    versions = rec.get("versions") if isinstance(rec.get("versions"), dict) else {}
    path = rtk.get("rtk_bin")
    if rtk.get("supported_version") != SUPPORTED_RTK_VERSION or versions.get("rtk") != SUPPORTED_RTK_VERSION:
        return None
    if not isinstance(path, str) or not os.path.isabs(path) or "\x00" in path:
        return None
    if os.path.normpath(path) != path or os.path.realpath(path) != path:
        return None
    owners = (os.getuid(), 0) if hasattr(os, "getuid") else None
    try:
        st = os.lstat(path)
        parent = os.lstat(os.path.dirname(path))
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID):
        return None
    if not os.access(path, os.X_OK):
        return None
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022:
        return None
    if owners is not None and (st.st_uid not in owners or parent.st_uid not in owners):
        return None
    return path


def _validated_private_rtk_dir(rdir):
    """This run's private raw/rtk folder (and the run-folder chain above it)
    as plain directories owned by us and not group/world-writable; the RTK
    paths inside must not be symlinks. Returns (rtk_dir, db_present) or
    raises ReportError with a fixed reason."""
    for path in (reports_root(), runs_dir(), rdir, os.path.join(rdir, "raw"), os.path.join(rdir, "raw", "rtk")):
        _owned_plain_dir(path, create=False)
    env_paths = rtk_env_for(rdir)
    for key, kind in (("RTK_TEE_DIR", stat.S_ISDIR), ("RTK_CONFIG_DIR", stat.S_ISDIR),
                      ("RTK_RECALL_DB", stat.S_ISREG), ("RTK_DB_PATH", stat.S_ISREG)):
        try:
            st = os.lstat(env_paths[key])
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not kind(st.st_mode):
            raise ReportError("a private RTK path of the run is not a plain file/directory")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise ReportError("a private RTK path of the run is not owned by the current user")
    return os.path.join(rdir, "raw", "rtk"), os.path.lexists(env_paths["RTK_DB_PATH"])


def _kill_child_group(proc, own_session=True):
    import signal
    if own_session:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5)


def bounded_capture(argv, env, cwd, timeout, max_bytes, cancel_fd=None, should_stop=None, own_session=True):
    """(returncode, stdout) of a child read incrementally while it runs: at
    most `max_bytes` are ever buffered and the deadline covers the whole
    run. stdin is /dev/null, stderr is discarded. POSIX only.

    own_session=True: the child gets a new session and its whole process
    group is killed on failure. own_session=False (the publisher): the child
    stays in the caller's process group — which the publisher leads, so the
    launcher can stop both together — and is killed directly. `cancel_fd`
    (readable = cancel) and `should_stop()` (polled at least once a second)
    cancel the run early. On timeout, overflow or cancellation the child is
    killed and reaped and ReportError is raised."""
    import selectors
    if os.name != "posix":
        raise ReportError("bounded capture is supported on POSIX systems only")
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, cwd=cwd, close_fds=True,
                                start_new_session=own_session)
    except OSError as ex:
        raise ReportError("could not run (%s)" % type(ex).__name__)
    deadline = time.monotonic() + timeout
    chunks, total, failure, rc = [], 0, None, None
    selector = selectors.DefaultSelector()
    try:
        fd = proc.stdout.fileno()
        selector.register(fd, selectors.EVENT_READ)
        if cancel_fd is not None:
            selector.register(cancel_fd, selectors.EVENT_READ)
        eof = False
        while not eof and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed out after %ss" % timeout
                break
            if should_stop is not None and should_stop():
                failure = "cancelled"
                break
            for key, _ in selector.select(min(remaining, 1.0)):
                if key.fd == cancel_fd:
                    failure = "cancelled"
                    break
                data = os.read(fd, 65536)
                if not data:
                    eof = True
                    break
                total += len(data)
                if total > max_bytes:
                    failure = "output too large"
                    break
                chunks.append(data)
        while failure is None:
            if should_stop is not None and should_stop():
                failure = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed out after %ss" % timeout
                break
            try:
                rc = proc.wait(timeout=min(remaining, 1.0))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        selector.close()
        with contextlib.suppress(OSError):
            proc.stdout.close()
        if proc.returncode is None:
            _kill_child_group(proc, own_session)
    if failure is not None:
        raise ReportError(failure)
    return rc, b"".join(chunks)


def _measure_active(rec, rdir, cancel_fd=None, should_stop=None):
    """RTK block of an active publication — at most one bounded `rtk gain
    --format json` (RTK_TIMEOUT_SECONDS, RTK_OUTPUT_MAX_BYTES of stdout) on
    this run's private database, with the re-validated recorded binary,
    every inherited RTK_* variable removed and only this run's private RTK
    paths set — or None on any failure, so the previous publication stays
    as it was and no fresher time is invented. Raw stdout is parsed here and
    never stored, logged or published."""
    measure = PUBLICATION_MEASUREMENTS["active-unfinalized"]
    try:
        rtk_dir, db_present = _validated_private_rtk_dir(rdir)
    except (ReportError, OSError):
        return None
    if not db_present:
        # RTK has not recorded any command for this launch yet.
        return _export_rtk("none-recorded", None, iso(now_epoch()), measure)
    rtk_bin = _validated_rtk_binary(rec)
    if rtk_bin is None:
        return None
    try:
        rc, data = bounded_capture([rtk_bin, "gain", "--format", "json"], sanitized_rtk_env(rtk_env_for(rdir)),
                                   rtk_dir, RTK_TIMEOUT_SECONDS, RTK_OUTPUT_MAX_BYTES, cancel_fd=cancel_fd,
                                   should_stop=should_stop, own_session=False)
    except ReportError:
        return None
    measured = iso(now_epoch())
    if rc != 0:
        return None
    clean, _reason = parse_gain_summary(data)
    if clean is None:
        return None
    return _export_rtk("available", clean, measured, measure)


def build_publication(run_entry):
    return {
        "schema": PUBLICATION_SCHEMA,
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "producer": {"name": EXPORT_PRODUCER, "helper_version": HELPER_VERSION,
                     "supported_rtk_version": SUPPORTED_RTK_VERSION},
        "published_at": iso(now_epoch()),
        "run": run_entry,
    }


def validate_publication(payload, run_id):
    """Pre-publication check of the exact public contract (keys, enums,
    ids, timestamps, numbers). Raises ReportError; never repairs."""
    def fail(what):
        raise ReportError("publication failed validation (%s); the previous publication was left unchanged" % what)

    if not isinstance(payload, dict) or set(payload) != {"schema", "schema_version", "producer", "published_at", "run"}:
        fail("top-level keys")
    if (payload["schema"] != PUBLICATION_SCHEMA or type(payload["schema_version"]) is not int
            or payload["schema_version"] != PUBLICATION_SCHEMA_VERSION):
        fail("schema")
    producer = payload["producer"]
    if (not isinstance(producer, dict) or set(producer) != {"name", "helper_version", "supported_rtk_version"}
            or producer["name"] != EXPORT_PRODUCER
            or not all(isinstance(producer[k], str) and VERSION_RE.match(producer[k])
                       for k in ("helper_version", "supported_rtk_version"))):
        fail("producer")
    if not _iso_or_none(payload["published_at"]):
        fail("published_at")
    run = payload["run"]
    run_keys = {"run_id", "mode", "record_state", "launch_kind", "created_at", "finalized_at", "session", "rtk"}
    stat_keys = set(RTK_SUMMARY_INT_KEYS) | {"avg_savings_pct"}
    if not isinstance(run, dict) or set(run) != run_keys:
        fail("run keys")
    if not isinstance(run["run_id"], str) or not RUN_ID_RE.match(run["run_id"]) or run["run_id"] != run_id:
        fail("run id")
    if run["mode"] != "rtk" or run["record_state"] not in EXPORT_RECORD_STATES:
        fail("run state")
    if run["launch_kind"] not in LAUNCH_KINDS + (None,):
        fail("launch kind")
    for key in ("created_at", "finalized_at"):
        if run[key] is not None and not _iso_or_none(run[key]):
            fail(key)
    if run["record_state"] == "active-unfinalized" and run["finalized_at"] is not None:
        fail("finalized_at")
    s = run["session"]
    if not isinstance(s, dict) or set(s) != {"link", "id", "proof", "corroborated_by"}:
        fail("session keys")
    if s["link"] == "confirmed":
        if not valid_session_id(s["id"]) or s["proof"] not in EXPORT_SESSION_PROOFS:
            fail("session proof")
    elif s["link"] != "unknown" or s["id"] is not None or s["proof"] is not None or s["corroborated_by"]:
        fail("unknown session")
    if (not isinstance(s["corroborated_by"], list)
            or len(set(s["corroborated_by"])) != len(s["corroborated_by"])
            or any(c not in EXPORT_CORROBORATIONS for c in s["corroborated_by"])
            or (s["proof"] == "single-conversation-id-in-own-otel-file"
                and "otel-conversation" in s["corroborated_by"])):
        fail("session evidence")
    r = run["rtk"]
    if not isinstance(r, dict) or set(r) != {"availability", "measurement", "measured_at", "stats"}:
        fail("rtk keys")
    if (r["availability"] not in EXPORT_RTK_AVAILABILITY
            or r["measurement"] != PUBLICATION_MEASUREMENTS[run["record_state"]]):
        fail("rtk availability")
    if r["measured_at"] is not None and not _iso_or_none(r["measured_at"]):
        fail("measured_at")
    if r["availability"] == "available":
        st = r["stats"]
        if not isinstance(st, dict) or set(st) != stat_keys:
            fail("stats keys")
        for key in RTK_SUMMARY_INT_KEYS:
            ok = strict_signed(st[key]) if key == "total_saved" else strict_count(st[key])
            if ok is None:
                fail("stats value")
        if st["avg_savings_pct"] is not None and finite_number(st["avg_savings_pct"]) is None:
            fail("stats percentage")
    elif r["stats"] is not None:
        fail("stats without availability")


def encode_publication(payload, run_id):
    """Validated, serialized publication (at most PUBLICATION_MAX_BYTES)."""
    validate_publication(payload, run_id)
    data = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > PUBLICATION_MAX_BYTES:
        raise ReportError("publication would be %d bytes (limit %d); nothing was written"
                          % (len(data), PUBLICATION_MAX_BYTES))
    return data


def _check_existing_target(target):
    """Refuse (never repair or replace) an existing file that is a symlink,
    not a regular single-link file, foreign-owned or group/world-writable."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ReportError("%s is not a regular single-link file (symlinks are refused); nothing was written"
                          % target)
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ReportError("%s is not owned by the current user; nothing was written" % target)
    if st.st_mode & 0o022:
        raise ReportError("%s is writable by group or others; refused (not repaired), nothing was written"
                          % target)


def _atomic_replace_bytes(data, directory, target):
    """Atomic 0600 write (temp file in `directory` + fsync + os.replace);
    the previous file stays in place on any failure."""
    _check_existing_target(target)
    fd, tmp = tempfile.mkstemp(prefix=PUBLICATION_TMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.lexists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)
    with contextlib.suppress(OSError):
        dfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _publication_target(runs, run_id):
    return os.path.join(runs, run_id + PUBLICATION_SUFFIX)


def _count_publications(runs):
    count = 0
    with os.scandir(runs) as it:
        for entry in it:
            if _publication_run_id(entry.name):
                count += 1
    return count


def _open_publication_runs_fd():
    """No-follow descriptor of the runs folder (every folder below home
    owned by us and not group/world-writable, checked on its descriptor)."""
    return _open_dir_nofollow(os.path.expanduser("~"),
                              [".copilot", EXPORT_DIR_NAME, PUBLICATION_VERSION_DIR, PUBLICATION_RUNS_DIR])


def _finalized_eviction_candidates(runs_fd, exclude):
    """[(finalized epoch, run id, (dev, ino))], oldest first, of existing
    publications that pass the full contract (private regular single-link
    file ≤ PUBLICATION_MAX_BYTES, read no-follow and checked on its
    descriptor, then validate_publication) with record_state `finalized`.
    Active, invalid, unreadable or unsafe files are never candidates. A
    finalized publication without a valid finalized_at sorts first."""
    names = []
    with os.scandir(runs_fd) as it:
        for entry in it:
            names.append(entry.name)
            if len(names) >= PUBLICATION_MAX_FILES * 4:
                break
    flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    candidates = []
    for name in names:
        run_id = _publication_run_id(name)
        if run_id is None or run_id == exclude:
            continue
        try:
            fd = os.open(name, flags, dir_fd=runs_fd)
        except OSError:
            continue
        try:
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022
                        or st.st_size > PUBLICATION_MAX_BYTES
                        or (hasattr(os, "getuid") and st.st_uid != os.getuid())):
                    continue
                data = f.read(PUBLICATION_MAX_BYTES + 1)
        except OSError:
            continue
        if len(data) > PUBLICATION_MAX_BYTES:
            continue
        try:
            payload = json.loads(data.decode("utf-8"))
            validate_publication(payload, run_id)
        except (ValueError, UnicodeDecodeError, RecursionError, ReportError, KeyError, TypeError, AttributeError):
            continue
        if payload["run"]["record_state"] != "finalized":
            continue
        finalized = _epoch_of_iso(payload["run"]["finalized_at"])
        candidates.append((finalized if finalized is not None else float("-inf"), run_id,
                           (st.st_dev, st.st_ino)))
    candidates.sort()
    return candidates


def _evict_oldest_finalized(exclude, needed):
    """Unlink up to `needed` of the oldest valid FINALIZED publications
    (never `exclude`, never an active one); each is re-checked by lstat to
    be the same regular file that was validated before it is unlinked by
    exact name relative to the runs-folder descriptor. Returns the number
    evicted. Caller holds the capacity lock."""
    if needed <= 0:
        return 0
    runs_fd = _open_publication_runs_fd()
    evicted = 0
    try:
        for _finalized, run_id, identity in _finalized_eviction_candidates(runs_fd, exclude):
            if evicted >= needed:
                break
            name = run_id + PUBLICATION_SUFFIX
            try:
                st = os.stat(name, dir_fd=runs_fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != identity:
                    continue
                os.unlink(name, dir_fd=runs_fd)
            except OSError:
                continue
            evicted += 1
    finally:
        os.close(runs_fd)
    return evicted


def write_publication(run_id, data, new_file_limit, evict=False):
    """Publish (replace) the run's own file under the capacity lock. A NEW
    file is refused once `new_file_limit` publications exist, unless
    `evict` (live publisher and `finish` only — never backfill) can first
    remove enough of the oldest finalized publications; active ones are
    never evicted, so with every slot active the write fails closed."""
    version, runs = ensure_publication_dirs()
    target = _publication_target(runs, run_id)
    with _try_exclusive_lock(version, PUBLICATION_CAPACITY_LOCK_NAME,
                             wait=PUBLICATION_CAPACITY_LOCK_WAIT_SECONDS) as locked:
        if not locked:
            raise ReportError("the publication folder is busy; nothing was written")
        _check_existing_target(target)
        if not os.path.lexists(target):
            count = _count_publications(runs)
            if count >= new_file_limit and evict:
                count -= _evict_oldest_finalized(run_id, count - new_file_limit + 1)
            if count >= new_file_limit:
                raise ReportError("publication limit (%d files) reached; nothing was written" % new_file_limit)
        _atomic_replace_bytes(data, runs, target)


def withdraw_publication(run_id):
    """Remove the run's publication (False when absent). Only that exact
    name is unlinked (a symlink there is removed, never followed); a
    directory there is refused."""
    dirs = existing_publication_dirs()
    if dirs is None:
        return False
    target = _publication_target(dirs[1], run_id)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        raise ReportError("a publication path is a directory; left unchanged")
    os.unlink(target)
    return True


def publish_finalized(rec, new_file_limit=PUBLICATION_MAX_FILES, evict=False):
    """Publish a finalized RTK-mode run from its stored record (no RTK).
    `evict` only for `finish` (see write_publication)."""
    data = encode_publication(build_publication(finalized_publication_run(rec)), rec["run_id"])
    write_publication(rec["run_id"], data, new_file_limit, evict=evict)


def publish_after_finish(rec):
    """Called by `finish` while it still holds the run lock, right after the
    finalized record was written. May evict the oldest finalized
    publication(s) to make room. Never raises and never changes the
    canonical finish result or output; a failure leaves one fixed notice on
    stderr and the next reconciliation slice retries."""
    try:
        if _publishable_rtk_record(rec):
            publish_finalized(rec, evict=True)
    except Exception:  # noqa: BLE001 — optional publication: never fails finish
        print("RTK publication for viewers not updated (retried automatically at a later launch).",
              file=sys.stderr)


# --------------------------------------------------------------------------
# rtk-publisher: the launch-scoped active publisher (started by copilot-s)
# --------------------------------------------------------------------------

def _publisher_cycle(run_id, rdir, evidence, cancel_fd, should_stop):
    """One publisher cycle: ("done" | "retry" | "published", evidence).
    "done" ends the publisher (record finalized or not publishable)."""
    try:
        rec = load_run_for_export(run_id)
    except ReportError:
        return "retry", evidence
    if _is_finalized(rec) or not _publishable_rtk_record(rec):
        return "done", evidence
    try:
        if evidence is None:
            evidence = _PublisherEvidence(rec)
        session = evidence.session_link(rec)
        rtk = _measure_active(rec, rdir, cancel_fd, should_stop)
        if rtk is None or should_stop():
            return "retry", evidence
        entry = _publication_entry_base(rec, "active-unfinalized")
        entry["session"] = session
        entry["rtk"] = rtk
        data = encode_publication(build_publication(entry), run_id)
        # Serialized with `finish` (which publishes the final figures while
        # holding this lock): an active result never replaces a final one.
        with run_lock(rdir, wait=PUBLISHER_LOCK_WAIT_SECONDS):
            if _is_finalized(load_run_for_export(run_id)):
                return "done", evidence
            write_publication(run_id, data, PUBLICATION_MAX_FILES, evict=True)
    except (ReportError, OSError, ValueError):
        return "retry", evidence
    return "published", evidence


def cmd_rtk_publisher(a):
    """Internal (started by copilot-s for one RTK launch; not a user
    command). Publishes the launch's active file about every
    PUBLISHER_INTERVAL_SECONDS until the record is finalized, the launcher
    stops it (SIGTERM/SIGHUP) or its parent is no longer the launcher
    (getppid checked at least once a second, so a re-parented publisher
    stops even if the launcher's PID is reused). It leads its own process
    group so the launcher can stop it together with a running `rtk gain`
    child; SIGINT (a terminal Ctrl-C meant for Copilot) is ignored. Writes
    nothing to stdout/stderr; exit 2 = refused to start."""
    import select
    import signal
    if not RUN_ID_RE.match(a.run_id or "") or a.parent_pid is None or a.parent_pid <= 1:
        return 2
    if os.getppid() != a.parent_pid:
        return 2
    with contextlib.suppress(OSError):
        os.setpgid(0, 0)
    stopping = []

    def on_stop(signum, _frame):
        stopping.append(signum)

    rfd, wfd = os.pipe()
    os.set_blocking(rfd, False)
    os.set_blocking(wfd, False)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGHUP, on_stop)
    signal.set_wakeup_fd(wfd)

    def should_stop():
        return bool(stopping) or os.getppid() != a.parent_pid

    def pause(seconds):
        """False when the publisher must stop."""
        deadline = time.monotonic() + seconds
        while not should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            with contextlib.suppress(OSError, ValueError):
                select.select([rfd], [], [], min(remaining, 1.0))
        return False

    rdir = run_dir(a.run_id)
    evidence = None
    if not pause(PUBLISHER_FIRST_DELAY_SECONDS):
        return 0
    while True:
        outcome, evidence = _publisher_cycle(a.run_id, rdir, evidence, rfd, should_stop)
        if outcome == "done" or should_stop():
            return 0
        if not pause(PUBLISHER_INTERVAL_SECONDS):
            return 0


# --------------------------------------------------------------------------
# Reconciliation of finalized runs (bounded, resumable; runs no RTK)
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _try_exclusive_lock(directory, name, wait=0):
    """Yields True holding an exclusive lock on directory/name (polling for
    up to `wait` seconds), False when another process still holds it."""
    import fcntl
    fd = os.open(os.path.join(directory, name), os.O_RDWR | os.O_CREAT | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022
                or (hasattr(os, "getuid") and st.st_uid != os.getuid())):
            raise ReportError("publication lock is not a private regular file owned by the current user")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def _read_cursor(version_dir):
    try:
        data = read_regular_file(os.path.join(version_dir, RECONCILE_CURSOR_NAME), 64)
    except OSError:
        return None
    if data is None:
        return None
    text = data.decode("ascii", "replace").strip()
    return text if RUN_ID_RE.match(text) else None


def _list_run_names(runs_fd):
    """(sorted run-id folder names, complete) — at most RECONCILE_MAX_NAMES."""
    names, complete = [], True
    with os.scandir(runs_fd) as it:
        for entry in it:
            if not RUN_ID_RE.match(entry.name):
                continue
            if len(names) >= RECONCILE_MAX_NAMES:
                complete = False
                break
            names.append(entry.name)
    names.sort()
    return names, complete


def _probe_run_folder(runs_fd, name):
    """(is RTK-mode candidate, source mtime or None) from metadata only,
    opened descriptor-relative without following symlinks: a candidate has a
    private run folder with raw/rtk-preflight/ (created only by RTK-mode
    `start`); the source mtime is the newest of run.json and the hook log."""
    try:
        rfd = os.open(name, _DIR_OPEN_FLAGS | _NOFOLLOW, dir_fd=runs_fd)
    except OSError:
        return False, None
    try:
        try:
            _check_private_dir_fd(rfd)
            raw = os.open("raw", _DIR_OPEN_FLAGS | _NOFOLLOW, dir_fd=rfd)
        except OSError:
            return False, None
        try:
            if not stat.S_ISDIR(os.stat("rtk-preflight", dir_fd=raw, follow_symlinks=False).st_mode):
                return False, None
        except OSError:
            return False, None
        finally:
            os.close(raw)
        try:
            mtime = os.stat("run.json", dir_fd=rfd, follow_symlinks=False).st_mtime
        except OSError:
            return True, None
        with contextlib.suppress(OSError):
            mtime = max(mtime, os.stat("hook-events.jsonl", dir_fd=rfd, follow_symlinks=False).st_mtime)
        return True, mtime
    finally:
        os.close(rfd)


def _sweep_publications(runs, known_runs, now, counts):
    """Withdraw publications not rewritten for PUBLICATION_RETENTION_SECONDS
    and — only for the default reports root with a complete listing —
    publications whose run folder no longer exists; remove leftover temp
    files older than PUBLICATION_TMP_MAX_AGE_SECONDS. Bounded listing."""
    limit = PUBLICATION_MAX_FILES * 4
    names = []
    with os.scandir(runs) as it:
        for entry in it:
            names.append(entry.name)
            if len(names) >= limit:
                break
    for name in sorted(names):
        path = os.path.join(runs, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            continue
        run_id = _publication_run_id(name)
        if run_id is None:
            if name.startswith(PUBLICATION_TMP_PREFIX) and st.st_mtime < now - PUBLICATION_TMP_MAX_AGE_SECONDS:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            continue
        if st.st_mtime < now - PUBLICATION_RETENTION_SECONDS or (known_runs is not None and run_id not in known_runs):
            with contextlib.suppress(OSError, ReportError):
                if withdraw_publication(run_id):
                    counts["withdrawn"] += 1


def _reconcile_one(run_id, source_mtime, runs, now, counts):
    target = _publication_target(runs, run_id)
    try:
        pub = os.lstat(target)
    except FileNotFoundError:
        pub = None
    if pub is not None and pub.st_mtime < now - PUBLICATION_RETENTION_SECONDS:
        if withdraw_publication(run_id):
            counts["withdrawn"] += 1
        return
    if source_mtime is None:
        if pub is not None and withdraw_publication(run_id):
            counts["withdrawn"] += 1
        return
    if pub is not None and pub.st_mtime >= source_mtime:
        return
    if pub is None and source_mtime < now - PUBLICATION_RETENTION_SECONDS:
        return
    counts["loaded"] += 1
    try:
        rec = load_run_for_export(run_id)
    except ReportError:
        rec = None
    if rec is None or not _publishable_rtk_record(rec):
        if pub is not None and withdraw_publication(run_id):
            counts["withdrawn"] += 1
        return
    if not _is_finalized(rec):
        return  # its live publisher owns it (or, if that launcher died, retention)
    finalized = _epoch_of_iso(rec.get("finalized_at"))
    if finalized is None or finalized < now - PUBLICATION_RETENTION_SECONDS:
        if pub is not None and withdraw_publication(run_id):
            counts["withdrawn"] += 1
        return
    # Backfill never evicts and never adds a new file at or above
    # BACKFILL_MAX_FILES (also if the file was evicted meanwhile); replacing
    # an existing file needs no new slot.
    publish_finalized(rec, BACKFILL_MAX_FILES)
    counts["published"] += 1


def reconcile_publications(time_budget=RECONCILE_TIME_BUDGET_SECONDS):
    """One bounded, resumable reconciliation slice (runs no RTK, never
    publishes active figures). Visits at most RECONCILE_SCAN_BUDGET run
    folders in run-id order starting after a small persisted cursor
    (wrapping around), loads at most RECONCILE_LOAD_BUDGET records, stops
    after `time_budget` seconds, and per RTK-mode candidate:

    - a publication not rewritten for the retention period is withdrawn;
    - one at least as new as its record and hook log is left alone;
    - otherwise the record is loaded (no-follow, bounded): one that is
      unreadable, not RTK mode or never launched has its publication
      withdrawn; an unfinalized one is left to its live publisher; a
      finalized one within retention is (re)published from stored
      statistics (new backfill files only while fewer than
      BACKFILL_MAX_FILES exist).

    Folders and the cursor/lock are created only when an RTK-mode run or
    the publication folder exists; a concurrent slice is skipped
    (non-waiting lock). Returns counts (numbers only)."""
    deadline = time.monotonic() + time_budget
    counts = {"probed": 0, "loaded": 0, "published": 0, "withdrawn": 0, "failed": 0}
    base, chain = _export_reports_chain()
    try:
        runs_fd = _open_dir_nofollow(base, chain + ["runs"])
    except FileNotFoundError:
        runs_fd = None
    except OSError:
        raise ReportError("the reports folder is not reachable through plain, private directories")
    try:
        names, complete = _list_run_names(runs_fd) if runs_fd is not None else ([], True)
        dirs = existing_publication_dirs()
        cursor = _read_cursor(dirs[0]) if dirs else None
        start = next((i for i, n in enumerate(names) if n > cursor), 0) if cursor else 0
        window = [names[(start + i) % len(names)] for i in range(min(RECONCILE_SCAN_BUDGET, len(names)))]
        probes = []
        for name in window:
            if time.monotonic() > deadline:
                break
            probes.append((name,) + _probe_run_folder(runs_fd, name))
        counts["probed"] = len(probes)
        if dirs is None and not any(candidate for _, candidate, _ in probes):
            return counts
        version_dir, runs = ensure_publication_dirs()
        with _try_exclusive_lock(version_dir, RECONCILE_LOCK_NAME) as locked:
            if not locked:
                return counts
            now = now_epoch()
            default_root = not os.environ.get("COPILOT_SESSION_REPORTS_DIR")
            known = None
            if default_root and runs_fd is not None:
                # Re-list under the lock: the pre-lock listing may predate a
                # run folder restored by copilot-report-cleanup.py (which
                # holds this lock while it moves runs and publications), and
                # must not make the sweep withdraw that run's publication.
                fresh, fresh_complete = _list_run_names(runs_fd)
                known = set(fresh) if fresh_complete else None
            _sweep_publications(runs, known, now, counts)
            last = None
            for name, candidate, source_mtime in probes:
                if time.monotonic() > deadline or counts["loaded"] >= RECONCILE_LOAD_BUDGET:
                    break
                last = name
                if not candidate:
                    continue
                try:
                    _reconcile_one(name, source_mtime, runs, now, counts)
                except (ReportError, OSError, ValueError):
                    counts["failed"] += 1
            if last is not None:
                with contextlib.suppress(ReportError, OSError):
                    _atomic_replace_bytes((last + "\n").encode("ascii"), version_dir,
                                          os.path.join(version_dir, RECONCILE_CURSOR_NAME))
    finally:
        if runs_fd is not None:
            os.close(runs_fd)
    return counts


def reconcile_quietly():
    """`start` hook: one slice; never fails `start`, never prints (the
    launcher parses start's stdout)."""
    with contextlib.suppress(Exception):
        reconcile_publications()


def cmd_rtk_publish_reconcile(a):
    """Internal/maintenance: run one reconciliation slice and print its
    counts (numbers only)."""
    print(json.dumps(reconcile_publications(), sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="copilot-session-report.py",
                                description="Per-launch session reports for copilot-s (normal and RTK mode).")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("preflight", help="read-only RTK-mode checks (no files written)")
    s.add_argument("--mode", choices=("rtk",), required=True)
    s.add_argument("--cwd", required=True)
    s.add_argument("--plugin-dir")
    s.add_argument("--extra-plugin-dir", action="append", default=[])
    s.add_argument("--session-cwd-uncertain", action="store_true")
    s.add_argument("--project-dir", action="append", default=[])
    s.add_argument("--project-dir-unknown")

    s = sub.add_parser("start", help="create this launch's run record (used by copilot-s)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--mode", choices=MODES, required=True)
    s.add_argument("--cwd", required=True)
    s.add_argument("--launch-kind", choices=LAUNCH_KINDS, required=True)
    s.add_argument("--resume-session-id")
    s.add_argument("--caller-selects-session", action="store_true")
    s.add_argument("--otel-file")
    s.add_argument("--usage-file-owner", choices=USAGE_FILE_OWNERS, required=True)
    s.add_argument("--plugin-dir")
    s.add_argument("--extra-plugin-dir", action="append", default=[])
    s.add_argument("--session-cwd-uncertain", action="store_true")
    s.add_argument("--project-dir", action="append", default=[])
    s.add_argument("--project-dir-unknown")
    s.add_argument("--copilot-s-version")

    s = sub.add_parser("finish", help="finalize this launch's run record (used by copilot-s)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--exit-code", type=int, required=True)
    s.add_argument("--launched-epoch", type=float)
    s.add_argument("--ended-epoch", type=float)
    s.add_argument("--session-id")
    s.add_argument("--session-state-dir")

    s = sub.add_parser("mark-outcome", help="record the task outcome of a run (explicit, user-supplied)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--outcome", choices=OUTCOMES, required=True)

    s = sub.add_parser("launch", help="show one run record (read-only)")
    s.add_argument("--run-id", required=True)
    s.add_argument("--format", choices=("text", "json"), default="text")

    s = sub.add_parser("session", help="all copilot-s launches linked to one session, by mode (read-only)")
    s.add_argument("--session-id", required=True)
    s.add_argument("--format", choices=("text", "json"), default="text")

    s = sub.add_parser("compare", help="all run records grouped by mode (read-only)")
    s.add_argument("--format", choices=("text", "json"), default="text")

    s = sub.add_parser(
        "rtk-publisher",
        help="internal: launch-scoped RTK publisher started and stopped by copilot-s (not a user command)",
        description=(
            "Internal. Started by copilot-s for one --rtk launch: republishes "
            "~/.copilot/%s/%s/%s/<run-id>.json about every %ss until the run is finalized, the launcher stops "
            "it or its parent is no longer the given launcher process."
            % (EXPORT_DIR_NAME, PUBLICATION_VERSION_DIR, PUBLICATION_RUNS_DIR, PUBLISHER_INTERVAL_SECONDS)))
    s.add_argument("--run-id", required=True)
    s.add_argument("--parent-pid", type=int, required=True)

    s = sub.add_parser(
        "rtk-publish-reconcile",
        help="internal/maintenance: one bounded reconciliation slice of finalized RTK publications (runs no RTK)")
    return p


def main(argv=None):
    os.umask(0o077)
    parser = build_parser()
    a = parser.parse_args(argv)
    handlers = {"preflight": cmd_preflight, "start": cmd_start, "finish": cmd_finish,
                "mark-outcome": cmd_mark_outcome, "launch": cmd_launch, "session": cmd_session,
                "compare": cmd_compare, "rtk-publisher": cmd_rtk_publisher,
                "rtk-publish-reconcile": cmd_rtk_publish_reconcile}
    if a.cmd not in handlers:
        parser.print_help(sys.stderr)
        return 2
    try:
        return handlers[a.cmd](a)
    except FileNotFoundError:
        print("copilot-session-report: run record not found", file=sys.stderr)
        return 1
    except (ReportError, OSError, ValueError) as ex:
        print("copilot-session-report: %s" % ex, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
