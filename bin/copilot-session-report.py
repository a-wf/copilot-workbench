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
def run_lock(rdir):
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return
    path = os.path.join(rdir, ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        deadline = time.monotonic() + 10
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
    try:
        summary = json.loads(data.decode("utf-8")).get("summary")
    except (ValueError, UnicodeDecodeError, AttributeError, RecursionError):
        summary = None
    if not isinstance(summary, dict):
        out["reason"] = "rtk gain returned an unexpected shape"
        return out
    clean = {}
    for key in RTK_SUMMARY_INT_KEYS:
        value = strict_signed(summary.get(key)) if key == "total_saved" else strict_count(summary.get(key))
        if value is None:
            out["reason"] = "rtk gain field %s is not a valid count" % key
            return out
        clean[key] = value
    pct = finite_number(summary.get("avg_savings_pct"))
    clean["avg_savings_pct"] = round(pct, 2) if pct is not None else None
    out.update(clean)
    out["availability"] = "available"
    return out


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
    return p


def main(argv=None):
    os.umask(0o077)
    parser = build_parser()
    a = parser.parse_args(argv)
    handlers = {"preflight": cmd_preflight, "start": cmd_start, "finish": cmd_finish,
                "mark-outcome": cmd_mark_outcome, "launch": cmd_launch, "session": cmd_session,
                "compare": cmd_compare}
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
