#!/usr/bin/env python3
"""copilot-report-cleanup.py — recoverable quarantine of finalized copilot-s
run reports and their private raw data, used by `copilot-s` (menu option `r`),
plus the explicit, IRREVERSIBLE purge of one quarantine batch (menu `r` ->
4; subcommands purge-list / purge-preview / purge, see below).

preview/apply/list/restore never delete, purge or clean up anything, and no
disk space is freed by them: eligible run folders are *renamed* (same
filesystem only, never copied, never over an existing path) into a private
batch directory with a durable manifest, so a batch can be restored later —
even after a crash part-way through. Only an explicit `apply` or
`restore --yes` changes anything there; only an explicit, interactively
confirmed `purge` deletes (one batch, nothing automatic).

Scope (fixed paths below the account home; nothing else is ever touched):
  - canonical run reports ~/.copilot/session-reports/runs/<run-id>/ — the
    whole per-launch folder: run.json, hook-events.jsonl and the private
    raw/ folder (Copilot's usage file, and for RTK launches RTK's own
    history DB, tee/recall stores, config and preflight folders);
  - for RTK launches, the run's own public publication
    ~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json.
  Every workspace/directory is covered: run records do not store the
  working directory, so there is no current-directory filter. Never
  touched: the global RTK database/config, Copilot's session-state, logs
  and OTEL files (paths recorded inside run.json are never used as
  targets), publications without a run report, and run reports under a
  custom COPILOT_SESSION_REPORTS_DIR (preview/apply refuse to run while it
  points anywhere else than the default root). Source paths are derived only
  from the home directory and a validated run id.

Quarantine: ~/.copilot/copilot-s-report-quarantine/<batch>/ (0700) with
  runs/<run-id>/, publications/<run-id>.json and manifest.json.

Subcommands (stdlib only):
  preview [--include-recent]
           Read-only (creates no file, folder or lock). Lists eligible runs
           with counts of kept runs by reason; each candidate is also a
           machine line "CANDIDATE<TAB><run-id><TAB><fingerprint>" (plus
           "<TAB>include-recent" when previewed with --include-recent).
  apply [--include-recent]
           Reads exactly those CANDIDATE lines, unmodified, on stdin (no
           other line format is accepted); re-validates each run under its
           locks and quarantines only unchanged, still-eligible runs.
  list     Read-only list of quarantine batches ("BATCH<TAB><batch>" lines).
  restore --batch B [--yes]
           Without --yes: read-only restore plan ("RESTORABLE<TAB><n>").
           With --yes: moves unchanged quarantined runs/publications back
           with a no-replace rename; refuses collisions and changed data.
           A batch with a purge journal (see below) is always refused.
  purge-list
           Read-only: every batch with its purge status, plus
           "PURGEABLE<TAB><batch>" for each fully settled candidate.
  purge-preview --batch B
           Read-only: full verification, exact inventory and estimated
           reclaimable space of ONE batch ("PURGE_SNAPSHOT" machine line).
  purge --batch B [--snapshot DIGEST]
           PERMANENTLY deletes ONE fully settled batch (every entry "moved"
           or "skipped", at least one moved; every moved run folder and
           publication unchanged since it was quarantined) after the exact
           phrase "PURGE reports <batch> <snapshot id>" is typed on the real
           terminal. Holds the quarantine .transaction.lock (which apply and
           restore hold for their whole run) for the destructive phase; the
           reconcile/run/capacity locks only guard canonical paths a purge
           never touches. Implemented by copilot_quarantine_purge.py next to
           this file (full protocol, journal, platform limits there).

Eligibility (fail closed on anything unexpected, regardless of age):
  - folder name is a lowercase v4 UUID; the folder, every directory and
    file below it are plain, owned by you, not group/world-writable, on the
    same filesystem, files single-link; no symlink, FIFO, socket or device;
    at most MAX_TREE_ENTRIES entries and MAX_TREE_DEPTH levels;
  - run.json (<= 8 MiB, read without following symlinks) has schema
    copilot-s.session-run with integer (not boolean) schema_version 1, the
    folder's run id, mode normal|rtk, a known launch kind, state
    completed|copilot-error|interrupted consistent with copilot_outcome,
    state_reason and an integer exit_code 0..255, and valid UTC
    created_at <= launched_at <= ended_at <= finalized_at. Incomplete
    (active or never finalized), report-failure, RTK preflight-failed,
    unknown and malformed records are always kept;
  - finalized, and every entry last changed, at least 24h ago (opt-in
    --include-recent waives only that minimum age; nothing may be in the
    future);
  - an RTK publication, if present, is a private regular single-link file
    <= 16 KiB of schema copilot-s.rtk-launch v1 for the same RTK run; a
    publication next to a normal-mode run keeps the run, and one with a
    future modification/change time always keeps it (any policy);
  - its run id is not in the current environment;
  - a supported open-file check (a system lsof, macOS/Linux) positively
    reports that no process has the run folder, any entry below it or its
    publication open. Unavailable/failed/timed-out/ambiguous checks keep it.

Apply lock order (all bounded waits; the kernel releases flocks if a holder
dies): quarantine .transaction.lock -> v1/.reconcile.lock (whole batch) ->
runs/<run-id>/.lock -> v1/.capacity.lock (global, last). The producer
(copilot-session-report.py) writes run records and publications only while
holding the run lock and re-reads the record by its canonical path after
acquiring it, reconciliation publishes only under the reconcile lock, and
every publication write holds the capacity lock — so no producer can
recreate a publication or write into a run folder once it was moved.
The publication is moved before the run folder (and back after it on
restore), so a publication is never left in place without its run.
"""

import argparse
import contextlib
import ctypes
import ctypes.util
import datetime
import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
import time
from collections import Counter

RUN_SCHEMA = "copilot-s.session-run"
RUN_SCHEMA_VERSION = 1
PUBLICATION_SCHEMA = "copilot-s.rtk-launch"
PUBLICATION_SCHEMA_VERSION = 1
MODES = ("normal", "rtk")
LAUNCH_KINDS = ("new", "resume")
FINAL_STATES = ("completed", "copilot-error", "interrupted")
PUBLICATION_RECORD_STATES = ("active-unfinalized", "finalized")

RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
BATCH_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

# Fixed locations below the account home (never configurable).
REPORTS_CHAIN = (".copilot", "session-reports", "runs")
EXPORT_CHAIN = (".copilot", "copilot-s-rtk-export", "v1")
PUBLICATION_RUNS_DIR = "runs"
PUBLICATION_SUFFIX = ".json"
QUARANTINE_CHAIN = (".copilot", "copilot-s-report-quarantine")
BATCH_RUNS_DIR = "runs"
BATCH_PUBLICATIONS_DIR = "publications"
MANIFEST_FILE = "manifest.json"
MANIFEST_KIND = "copilot-s.report-quarantine"
MANIFEST_VERSION = 1

RUN_RECORD_FILE = "run.json"
RUN_LOCK_NAME = ".lock"                    # copilot-session-report.py run_lock
TRANSACTION_LOCK_NAME = ".transaction.lock"
RECONCILE_LOCK_NAME = ".reconcile.lock"    # copilot-session-report.py RECONCILE_LOCK_NAME
CAPACITY_LOCK_NAME = ".capacity.lock"      # copilot-session-report.py PUBLICATION_CAPACITY_LOCK_NAME
PUBLICATION_MAX_FILES = 256                # copilot-session-report.py PUBLICATION_MAX_FILES

MIN_AGE_SECONDS = 24 * 3600
INCLUDE_RECENT_MARKER = "include-recent"
CANDIDATE_TAG = "CANDIDATE"

# Bounds.
RUN_RECORD_MAX_BYTES = 8 * 1024 * 1024
PUBLICATION_MAX_BYTES = 16 * 1024
MANIFEST_MAX_BYTES = 4 * 1024 * 1024
STDIN_MAX_BYTES = 64 * 1024
MAX_LISTED_NAMES = 100000
MAX_BATCH_ENTRIES = 100
MAX_LISTED_BATCHES = 1000
MAX_PUBLICATION_COUNT_SCAN = PUBLICATION_MAX_FILES * 4
MAX_TREE_ENTRIES = 4096
MAX_TREE_DEPTH = 16
READ_BUDGET_BYTES = 512 * 1024 * 1024
PREVIEW_TIME_BUDGET_SECONDS = 180
APPLY_TIME_BUDGET_SECONDS = 900
LSOF_TIMEOUT_SECONDS = 15
LSOF_MAX_PATHS = 200
TRANSACTION_LOCK_WAIT_SECONDS = 10
RECONCILE_LOCK_WAIT_SECONDS = 10
RUN_LOCK_WAIT_SECONDS = 2
CAPACITY_LOCK_WAIT_SECONDS = 5
REASON_MAX_CHARS = 300

ENTRY_STATES = ("requested", "skipped", "pending", "pub_moved", "moved", "failed",
                "restoring", "run_restored", "restored")

# Permanent purge (purge-* subcommands only). The engine is loaded lazily
# from this file's real directory, so every other subcommand works without
# it. PURGE_CONTROL_DIR mirrors copilot_quarantine_purge.CONTROL_DIR.
PURGE_ENGINE_FILE = "copilot_quarantine_purge.py"
PURGE_CONTROL_DIR = ".purge"
PURGE_MANIFEST_KEYS = frozenset(["kind", "version", "batch", "created_at", "include_recent", "entries"])
PURGE_ENTRY_KEYS = frozenset(["id", "fingerprint", "state", "reason", "mode", "tree_fp", "pub_fp", "bytes",
                              "finalized_at"])

REASON_GONE = "run folder no longer exists"
REASON_UNSAFE = "unsafe folder or file (symlink, owner, permissions, links, type or filesystem)"
REASON_UNREADABLE = "unreadable"
REASON_CHANGING = "changed while being inspected"
REASON_CHANGED = "changed since the preview"
REASON_TOO_LARGE = "too many files or folders to inspect safely"
REASON_TOO_DEEP = "folder nesting too deep to inspect safely"
REASON_NO_RECORD = "no run.json (not a run report)"
REASON_RECORD_TOO_LARGE = "run.json too large"
REASON_MALFORMED = "run.json malformed or not a supported run record"
REASON_ACTIVE = "not finalized (active launch, interrupted launcher or failed RTK preflight)"
REASON_REPORT_FAILURE = "finalization failed (report-failure)"
REASON_UNKNOWN_STATE = "unknown run state"
REASON_INCONSISTENT = "state, outcome and exit status are inconsistent"
REASON_TIMES = "timestamps missing, invalid or out of order"
REASON_FUTURE = "timestamp in the future (clock or metadata problem)"
REASON_RECENT = "finalized within the last 24h"
REASON_RECENT_CHANGE = "files changed within the last 24h"
REASON_PUB_UNSAFE = "its RTK publication is not a private regular file"
REASON_PUB_UNKNOWN = "its RTK publication is not a recognized publication of this run"
REASON_PUB_UNEXPECTED = "a publication exists for a normal-mode run"
REASON_PUB_FUTURE = "its RTK publication has a timestamp in the future (clock problem or a new write)"
REASON_ENV = "referenced by the current environment (possibly an active launch)"
REASON_OPEN = "open by a running process"
REASON_BUDGET = "not inspected (time or read budget reached; run the cleanup again)"
REASON_OVER_LIMIT = "eligible, but over this batch's limit of %d runs (run the cleanup again)" % MAX_BATCH_ENTRIES
REASON_BUSY = "run record busy (its lock is held by another copilot-s process)"
REASON_PUB_BUSY = "publication folder busy (its capacity lock is held)"

DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
FILE_READ_FLAGS = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)

# Atomic no-replace rename flags.
RENAME_EXCL_DARWIN = 0x00000004
RENAME_NOREPLACE_LINUX = 1
SYS_RENAMEAT2 = {"x86_64": 316, "amd64": 316, "aarch64": 276, "arm64": 276}

# Only these fixed system locations are used for the open-file check (no
# PATH lookup).
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof", "/bin/lsof")


class CleanupError(Exception):
    """A fatal, user-visible error: nothing further is attempted."""


class Problem(Exception):
    """One run (or quarantined entry) is not eligible; args[0] = reason."""

    @property
    def reason(self):
        return self.args[0]


class ManifestError(Exception):
    """The manifest could not be saved durably (stop before further moves)."""


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_epoch():
    return time.time()


def home_dir():
    home = os.path.expanduser("~")
    if not home or home == "/" or not os.path.isabs(home) or os.path.normpath(home) != home:
        raise CleanupError("cannot determine your home directory (HOME must be an absolute, normalized path)")
    return home


def display_path(path):
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def human_bytes(count):
    value = float(count)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return ("%d %s" % (count, unit)) if unit == "bytes" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%d bytes" % count


def epoch_of_iso(value):
    if not isinstance(value, str) or not ISO_UTC_RE.match(value):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def outcome_from_exit(code):
    """Same mapping as copilot-session-report.py outcome_from_exit."""
    if code == 0:
        return "completed", "copilot-exited-0"
    if code in (129, 130, 131, 143):
        return "interrupted", "copilot-exited-on-signal-%d" % (code - 128)
    if code in (126, 127):
        return "copilot-error", "copilot-launch-failed-%d" % code
    return "copilot-error", "copilot-exited-%d" % code


def strict_int(value):
    return type(value) is int


def sha256_json(obj):
    data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def close_fds(*fds):
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def fsync_fd(fd):
    if fd is None:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass


def lexists_at(dir_fd, name):
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _stat_key(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read_limited(fd, limit):
    """Read at most limit + 1 bytes (so an oversized file is detectable)."""
    chunks, remaining = [], limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _private_dir_ok(st):
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022)


def open_dir_at(parent_fd, name, label):
    """No-follow descriptor of parent_fd/name: a directory owned by you and
    not group/world-writable (checked on the open descriptor). Raises
    FileNotFoundError when missing, CleanupError when unsafe."""
    try:
        fd = os.open(name, DIR_FLAGS | NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError:
        raise CleanupError("%s is not a plain directory (symlinks are refused)" % label)
    try:
        if not _private_dir_ok(os.fstat(fd)):
            raise CleanupError("%s is not a directory owned by you that only you can write (refused, not repaired)"
                               % label)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_home_chain(names, create=False):
    """Descriptor of ~/names... or None when a component is missing (and
    `create` is False). The home directory is opened as given and must be
    yours and not group/world-writable; every component below it is opened
    with O_NOFOLLOW relative to its parent and checked the same way.
    `create` makes missing components 0700 (explicit apply/restore only)."""
    home = home_dir()
    try:
        fd = os.open(home, DIR_FLAGS)
    except OSError:
        raise CleanupError("cannot open your home directory")
    try:
        if not _private_dir_ok(os.fstat(fd)):
            raise CleanupError("your home directory is not owned by you or is group/world-writable; refused")
        label = "~"
        for name in names:
            label += "/" + name
            try:
                nfd = open_dir_at(fd, name, label)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    fd = None
                    return None
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise CleanupError("cannot create %s: %s" % (label, exc.strerror))
                nfd = open_dir_at(fd, name, label)
            os.close(fd)
            fd = nfd
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def check_reports_root(home):
    """Refuse (change nothing) when COPILOT_SESSION_REPORTS_DIR points to a
    custom root: only the default ~/.copilot/session-reports is managed."""
    override = os.environ.get("COPILOT_SESSION_REPORTS_DIR")
    default = os.path.join(home, ".copilot", "session-reports")
    if override and os.path.abspath(override) != default:
        raise CleanupError("COPILOT_SESSION_REPORTS_DIR points to a custom reports root (%s); this cleanup only "
                           "manages the default ~/.copilot/session-reports and changed nothing"
                           % display_path(os.path.abspath(override)))


# --------------------------------------------------------------------------
# Locks (flock; the kernel releases them when the holder exits)
# --------------------------------------------------------------------------

def acquire_lock(dir_fd, name, wait):
    """Exclusive flock on dir_fd/name, the file created 0600 if missing and
    never opened through a symlink; it must be a private regular single-link
    file owned by you. Returns the descriptor, or None while another process
    still holds it after `wait` seconds. Raises Problem when unusable."""
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dir_fd)
    except OSError:
        raise Problem("lock file %s cannot be opened safely" % name)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                or st.st_mode & 0o022):
            raise Problem("lock file %s is not a private regular file" % name)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                pass
            except OSError:
                raise Problem("lock file %s cannot be locked" % name)
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)
    except BaseException:
        close_fds(fd)
        raise


def acquire_global_lock(dir_fd, name, wait, what):
    try:
        fd = acquire_lock(dir_fd, name, wait)
    except Problem as exc:
        raise CleanupError("%s: %s; nothing was changed" % (what, exc.reason))
    if fd is None:
        raise CleanupError("%s is busy (another process holds %s); nothing was changed — try again later"
                           % (what, name))
    return fd


def acquire_run_lock(parent_fd, run_id):
    """The producer's own run lock (<run folder>/.lock), opened relative to
    a no-follow descriptor of the run folder. Returns the descriptor, or
    None when busy. Raises Problem when the folder or lock is unusable."""
    try:
        rfd = os.open(run_id, DIR_FLAGS | NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise Problem(REASON_GONE)
    except OSError:
        raise Problem(REASON_UNSAFE)
    try:
        if not _private_dir_ok(os.fstat(rfd)):
            raise Problem(REASON_UNSAFE)
        return acquire_lock(rfd, RUN_LOCK_NAME, RUN_LOCK_WAIT_SECONDS)
    finally:
        os.close(rfd)


def own_lock_fact(fd):
    """The verified (pid, descriptor, inode) of one of our own lock
    descriptors — the only lsof records that are not treated as activity."""
    return (os.getpid(), str(fd), os.fstat(fd).st_ino)


# --------------------------------------------------------------------------
# Open-file check (system lsof only; fails closed)
# --------------------------------------------------------------------------

def find_lsof():
    for candidate in LSOF_CANDIDATES:
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if (stat.S_ISREG(st.st_mode) and os.access(candidate, os.X_OK) and st.st_uid == 0
                and not st.st_mode & 0o022):
            return candidate
    return None


def run_lsof(argv, timeout):
    """The only place lsof is executed (test seam: replace this function)."""
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})


def parse_lsof_fields(text):
    """[(pid, descriptor, inode or None)] from `lsof -F pfi` output, or None
    when any line is not understood. A process line without any file line
    yields one (pid, None, None) record."""
    records, pid, pid_has_file = [], None, True
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            if not value.isdigit():
                return None
            if pid is not None and not pid_has_file:
                records.append([pid, None, None])
            pid, pid_has_file = int(value), False
        elif tag == "f":
            if pid is None or not value:
                return None
            records.append([pid, value, None])
            pid_has_file = True
        elif tag == "i":
            if not records or records[-1][1] is None or not value.isdigit():
                return None
            records[-1][2] = int(value)
        else:
            return None
    if pid is not None and not pid_has_file:
        records.append([pid, None, None])
    return [tuple(r) for r in records]


def open_state(paths, exempt=frozenset(), deadline=None):
    """'closed', 'open' or 'unknown: <why>' for the given absolute paths
    (every file AND folder is passed explicitly; lsof does not recurse).
    Only a clean result counts as 'closed': exit 1 with empty stdout and
    stderr, or exit 0/1 with empty stderr where every reported record is
    one of our own verified lock descriptors (`exempt`)."""
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return "unknown: unsupported platform %s" % sys.platform
    lsof = find_lsof()
    if lsof is None:
        return "unknown: no system lsof found"
    paths = list(paths)
    for start in range(0, len(paths), LSOF_MAX_PATHS):
        chunk = paths[start:start + LSOF_MAX_PATHS]
        timeout = LSOF_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                return "unknown: time budget reached"
            timeout = min(timeout, remaining)
        try:
            result = run_lsof([lsof, "-n", "-P", "-w", "-F", "pfi", "--"] + chunk, timeout)
        except subprocess.TimeoutExpired:
            return "unknown: lsof timed out"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return "unknown: lsof failed (%s)" % type(exc).__name__
        out = result.stdout.decode("ascii", "replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        err = result.stderr.decode("ascii", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        records = parse_lsof_fields(out)
        if records is None:
            if any(line.startswith("p") for line in out.splitlines()):
                return "open"
            return "unknown: lsof output not understood"
        if any(record not in exempt for record in records):
            return "open"
        if records:
            # Every record is one of our own verified lock descriptors
            # (exact pid, descriptor AND inode; a record without an inode
            # or file is never exempt). lsof exits 1 when some of the
            # given paths are not open at all, 0 when all are — both are
            # clean only with empty stderr.
            if result.returncode in (0, 1) and not err.strip():
                continue
            return "unknown: lsof exit %d" % result.returncode
        if result.returncode == 1 and not out.strip() and not err.strip():
            continue
        return "unknown: lsof exit %d" % result.returncode
    return "closed"


# --------------------------------------------------------------------------
# Atomic no-replace rename (same primitive as copilot-session-cleanup.py)
# --------------------------------------------------------------------------

_RENAME_FUNC = None


def _no_replace_rename_func():
    """Resolve the platform's atomic no-replace renameat, or raise
    CleanupError (nothing is moved without it)."""
    global _RENAME_FUNC
    if _RENAME_FUNC is not None:
        return _RENAME_FUNC
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError as exc:
        raise CleanupError("cannot load the C library for a no-replace rename: %s" % exc)
    argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if sys.platform == "darwin":
        func = getattr(libc, "renameatx_np", None)
        if func is None:
            raise CleanupError("renameatx_np is unavailable (macOS 10.12+ required); nothing was moved")
        func.argtypes, func.restype = argtypes, ctypes.c_int

        def rename(src_fd, src, dst_fd, dst):
            return func(src_fd, src, dst_fd, dst, RENAME_EXCL_DARWIN)
    elif sys.platform.startswith("linux"):
        func = getattr(libc, "renameat2", None)
        if func is not None:
            func.argtypes, func.restype = argtypes, ctypes.c_int

            def rename(src_fd, src, dst_fd, dst):
                return func(src_fd, src, dst_fd, dst, RENAME_NOREPLACE_LINUX)
        else:
            number = SYS_RENAMEAT2.get(platform.machine().lower())
            syscall = getattr(libc, "syscall", None)
            if number is None or syscall is None:
                raise CleanupError("renameat2 is unavailable on this system; nothing was moved")
            syscall.restype = ctypes.c_long

            def rename(src_fd, src, dst_fd, dst):
                return syscall(ctypes.c_long(number), ctypes.c_int(src_fd), ctypes.c_char_p(src),
                               ctypes.c_int(dst_fd), ctypes.c_char_p(dst),
                               ctypes.c_uint(RENAME_NOREPLACE_LINUX))
    else:
        raise CleanupError("no atomic no-replace rename on platform %s; nothing was moved" % sys.platform)
    _RENAME_FUNC = rename
    return rename


def rename_noreplace_at(src_fd, src_name, dst_fd, dst_name):
    """Atomically rename src_fd/src_name to dst_fd/dst_name, failing with
    OSError (EEXIST/ENOTEMPTY) instead of replacing anything at the target."""
    rename = _no_replace_rename_func()
    if rename(src_fd, os.fsencode(src_name), dst_fd, os.fsencode(dst_name)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def move_error_text(exc):
    if exc.errno == errno.EXDEV:
        return "different filesystem"
    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
        return "target already exists"
    if exc.errno in (errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1)):
        return "no-replace rename not supported here (%s)" % (exc.strerror or exc)
    return exc.strerror or str(exc)


# --------------------------------------------------------------------------
# Inspection: run folder tree, run record, publication
# --------------------------------------------------------------------------

class Context(object):
    """Open descriptors of the fixed source folders plus per-command budgets."""

    def __init__(self, include_recent=False, budget=PREVIEW_TIME_BUDGET_SECONDS):
        self.home = home_dir()
        self.include_recent = bool(include_recent)
        self.deadline = time.monotonic() + budget
        self.read_left = READ_BUDGET_BYTES
        self.env_blob = "\0".join(os.environ.values())
        self.runs_fd = self.export_fd = self.pub_runs_fd = None

    def open_sources(self, check_root=True):
        if check_root:
            check_reports_root(self.home)
        self.runs_fd = open_home_chain(REPORTS_CHAIN)
        self.open_export(create=False)

    def open_runs(self, create):
        if self.runs_fd is None:
            self.runs_fd = open_home_chain(REPORTS_CHAIN, create=create)

    def open_export(self, create):
        if self.export_fd is None:
            self.export_fd = open_home_chain(EXPORT_CHAIN, create=create)
        if self.export_fd is not None and self.pub_runs_fd is None:
            label = "~/" + "/".join(EXPORT_CHAIN + (PUBLICATION_RUNS_DIR,))
            try:
                self.pub_runs_fd = open_dir_at(self.export_fd, PUBLICATION_RUNS_DIR, label)
            except FileNotFoundError:
                if not create:
                    return
                try:
                    os.mkdir(PUBLICATION_RUNS_DIR, 0o700, dir_fd=self.export_fd)
                except FileExistsError:
                    pass
                self.pub_runs_fd = open_dir_at(self.export_fd, PUBLICATION_RUNS_DIR, label)

    def check_time(self):
        if time.monotonic() > self.deadline:
            raise Problem(REASON_BUDGET)

    def charge(self, count):
        self.read_left -= count
        if self.read_left < 0:
            raise Problem(REASON_BUDGET)

    def run_path(self, run_id, rel=()):
        return os.path.join(self.home, *(REPORTS_CHAIN + (run_id,) + tuple(rel)))

    def pub_path(self, run_id):
        return os.path.join(self.home, *(EXPORT_CHAIN + (PUBLICATION_RUNS_DIR, run_id + PUBLICATION_SUFFIX)))

    def close(self):
        close_fds(self.pub_runs_fd, self.export_fd, self.runs_fd)
        self.runs_fd = self.export_fd = self.pub_runs_fd = None


def _read_record(ctx, dir_fd, lst):
    """(bytes, sha256) of run.json, read no-follow and checked unchanged."""
    if lst.st_size > RUN_RECORD_MAX_BYTES:
        raise Problem(REASON_RECORD_TOO_LARGE)
    ctx.charge(lst.st_size)
    fd = os.open(RUN_RECORD_FILE, FILE_READ_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise Problem(REASON_CHANGING)
        data = _read_limited(fd, RUN_RECORD_MAX_BYTES)
        if _stat_key(os.fstat(fd)) != _stat_key(st):
            raise Problem(REASON_CHANGING)
    finally:
        os.close(fd)
    if len(data) > RUN_RECORD_MAX_BYTES:
        raise Problem(REASON_RECORD_TOO_LARGE)
    return data, hashlib.sha256(data).hexdigest()


def _scan_dir(ctx, dfd, rel, depth, acc):
    uid = os.getuid()
    before = os.fstat(dfd)
    names = sorted(os.listdir(dfd))
    for name in names:
        ctx.check_time()
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            raise Problem(REASON_CHANGING)
        relp = rel + (name,)
        if depth == 0 and name == RUN_LOCK_NAME:
            # The producer's lock file: validated and passed to the open-file
            # check, but not fingerprinted (apply may create it).
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1
                    or st.st_mode & 0o022 or st.st_dev != acc["dev"]):
                raise Problem(REASON_UNSAFE)
            acc["rels"].append(relp)
            continue
        acc["count"] += 1
        if acc["count"] > MAX_TREE_ENTRIES:
            raise Problem(REASON_TOO_LARGE)
        if st.st_uid != uid or st.st_mode & 0o022 or st.st_dev != acc["dev"]:
            raise Problem(REASON_UNSAFE)
        hexrel = "/".join(os.fsencode(part).hex() for part in relp)
        changed = max(st.st_mtime, st.st_ctime)
        acc["newest"] = changed if acc["newest"] is None else max(acc["newest"], changed)
        acc["rels"].append(relp)
        if stat.S_ISDIR(st.st_mode):
            if depth + 1 >= MAX_TREE_DEPTH:
                raise Problem(REASON_TOO_DEEP)
            cfd = os.open(name, DIR_FLAGS | NOFOLLOW, dir_fd=dfd)
            try:
                cst = os.fstat(cfd)
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISDIR(cst.st_mode):
                    raise Problem(REASON_CHANGING)
                acc["records"].append(["d", hexrel, st.st_ino, st.st_mode, st.st_uid, st.st_nlink,
                                       st.st_mtime_ns, st.st_ctime_ns])
                _scan_dir(ctx, cfd, relp, depth + 1, acc)
            finally:
                os.close(cfd)
        elif stat.S_ISREG(st.st_mode):
            if st.st_nlink != 1:
                raise Problem(REASON_UNSAFE)
            acc["bytes"] += st.st_size
            acc["records"].append(["f", hexrel, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size,
                                   st.st_mtime_ns, st.st_ctime_ns])
            if depth == 0 and name == RUN_RECORD_FILE:
                acc["record"] = _read_record(ctx, dfd, st)
        else:
            raise Problem(REASON_UNSAFE)
    after = os.fstat(dfd)
    if (after.st_mtime_ns != before.st_mtime_ns or after.st_ino != before.st_ino
            or sorted(os.listdir(dfd)) != names):
        raise Problem(REASON_CHANGING)


def scan_run_tree(ctx, parent_fd, run_id):
    """Inspect parent_fd/<run_id>/ entirely through no-follow descriptors.
    Returns {"tree_fp", "bytes", "newest", "rels", "record"} or raises
    Problem. The tree fingerprint covers every entry's relative name, type,
    inode, mode, owner, links, size and times plus run.json's SHA-256; it
    excludes only the run folder's own times/link count and the top-level
    .lock file, which a rename or lock acquisition may change."""
    try:
        root_fd = os.open(run_id, DIR_FLAGS | NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        raise Problem(REASON_GONE)
    except OSError:
        raise Problem(REASON_UNSAFE)
    try:
        rst = os.fstat(root_fd)
        if not _private_dir_ok(rst) or rst.st_dev != os.fstat(parent_fd).st_dev:
            raise Problem(REASON_UNSAFE)
        acc = {"records": [["root", rst.st_ino, rst.st_mode, rst.st_uid]], "rels": [()], "bytes": 0,
               "newest": None, "count": 0, "record": None, "dev": rst.st_dev}
        try:
            _scan_dir(ctx, root_fd, (), 0, acc)
        except OSError:
            raise Problem(REASON_UNREADABLE)
    finally:
        os.close(root_fd)
    if acc["record"] is None:
        raise Problem(REASON_NO_RECORD)
    data, digest = acc["record"]
    tree_fp = sha256_json({"v": 1, "entries": acc["records"], "run_json_sha256": digest})
    return {"tree_fp": tree_fp, "bytes": acc["bytes"], "newest": acc["newest"], "rels": acc["rels"],
            "record": data}


def record_eligibility(data, run_id, now, include_recent):
    """(reason or None, summary) for a run record's bytes (strict)."""
    summary = {}
    try:
        rec = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return REASON_MALFORMED, summary
    if (not isinstance(rec, dict) or rec.get("schema") != RUN_SCHEMA
            or not strict_int(rec.get("schema_version")) or rec.get("schema_version") != RUN_SCHEMA_VERSION
            or rec.get("run_id") != run_id or rec.get("mode") not in MODES):
        return REASON_MALFORMED, summary
    summary["mode"] = rec["mode"]
    state = rec.get("state")
    summary["state"] = state if isinstance(state, str) and len(state) <= 40 else None
    session = rec.get("session") if isinstance(rec.get("session"), dict) else {}
    sid = session.get("id")
    summary["session"] = sid if isinstance(sid, str) and SESSION_ID_RE.match(sid) else None
    if state == "incomplete":
        return REASON_ACTIVE, summary
    if state == "report-failure":
        return REASON_REPORT_FAILURE, summary
    if state not in FINAL_STATES:
        return REASON_UNKNOWN_STATE, summary
    launch = rec.get("launch")
    if not isinstance(launch, dict) or launch.get("kind") not in LAUNCH_KINDS:
        return REASON_MALFORMED, summary
    code = rec.get("exit_code")
    if not strict_int(code) or not 0 <= code <= 255:
        return REASON_INCONSISTENT, summary
    expected = outcome_from_exit(code)
    if rec.get("copilot_outcome") != state or (state, rec.get("state_reason")) != expected:
        return REASON_INCONSISTENT, summary
    times = [epoch_of_iso(rec.get(key)) for key in ("created_at", "launched_at", "ended_at", "finalized_at")]
    if any(t is None for t in times) or times != sorted(times):
        return REASON_TIMES, summary
    summary["finalized_at"] = rec["finalized_at"]
    if rec["mode"] == "rtk":
        rtk = rec.get("rtk") if isinstance(rec.get("rtk"), dict) else {}
        preflight = rtk.get("preflight") if isinstance(rtk.get("preflight"), dict) else {}
        if preflight.get("smoke_test") != "passed":
            return REASON_MALFORMED, summary
    finalized = times[3]
    if finalized > now:
        return REASON_FUTURE, summary
    if not include_recent and finalized > now - MIN_AGE_SECONDS:
        return REASON_RECENT, summary
    return None, summary


def read_publication(ctx, pub_dir_fd, run_id):
    """None when the run has no publication there; else {"fp", "bytes"}.
    Raises Problem when it is unsafe or not a publication of this RTK run.
    The fingerprint excludes ctime (a rename changes it)."""
    name = run_id + PUBLICATION_SUFFIX
    try:
        lst = os.stat(name, dir_fd=pub_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise Problem(REASON_PUB_UNSAFE)
    if (not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1 or lst.st_uid != os.getuid()
            or lst.st_mode & 0o022 or lst.st_size > PUBLICATION_MAX_BYTES):
        raise Problem(REASON_PUB_UNSAFE)
    ctx.charge(lst.st_size)
    try:
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=pub_dir_fd)
    except OSError:
        raise Problem(REASON_PUB_UNSAFE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
            raise Problem(REASON_CHANGING)
        data = _read_limited(fd, PUBLICATION_MAX_BYTES)
        if _stat_key(os.fstat(fd)) != _stat_key(st):
            raise Problem(REASON_CHANGING)
    except OSError:
        raise Problem(REASON_PUB_UNSAFE)
    finally:
        os.close(fd)
    if len(data) > PUBLICATION_MAX_BYTES:
        raise Problem(REASON_PUB_UNSAFE)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Problem(REASON_PUB_UNKNOWN)
    run = payload.get("run") if isinstance(payload, dict) else None
    if (not isinstance(run, dict) or payload.get("schema") != PUBLICATION_SCHEMA
            or not strict_int(payload.get("schema_version"))
            or payload.get("schema_version") != PUBLICATION_SCHEMA_VERSION
            or run.get("run_id") != run_id or run.get("mode") != "rtk"
            or run.get("record_state") not in PUBLICATION_RECORD_STATES):
        raise Problem(REASON_PUB_UNKNOWN)
    fp = sha256_json(["pub", st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size, st.st_mtime_ns,
                      hashlib.sha256(data).hexdigest()])
    return {"fp": fp, "bytes": st.st_size, "changed": max(st.st_mtime, st.st_ctime)}


def combined_fingerprint(tree_fp, pub_fp, mode):
    return sha256_json({"v": 1, "tree": tree_fp, "publication": pub_fp, "mode": mode})


def inspect_run(ctx, run_id, check_activity=False, exempt=frozenset()):
    """Full eligibility of one canonical run (fresh time each call).
    Returns an info dict; info["reason"] is None only when eligible."""
    info = {"id": run_id, "reason": None, "mode": None, "state": None, "finalized_at": None, "session": None,
            "bytes": 0, "has_pub": False, "tree_fp": None, "pub_fp": None, "fingerprint": None}
    try:
        ctx.check_time()
        tree = scan_run_tree(ctx, ctx.runs_fd, run_id)
        now = now_epoch()
        reason, summary = record_eligibility(tree["record"], run_id, now, ctx.include_recent)
        info.update(summary)
        if reason:
            raise Problem(reason)
        newest = tree["newest"]
        if newest is not None:
            if newest > now:
                raise Problem(REASON_FUTURE)
            if not ctx.include_recent and newest > now - MIN_AGE_SECONDS:
                raise Problem(REASON_RECENT_CHANGE)
        pub = read_publication(ctx, ctx.pub_runs_fd, run_id) if ctx.pub_runs_fd is not None else None
        if pub is not None and info["mode"] != "rtk":
            raise Problem(REASON_PUB_UNEXPECTED)
        if pub is not None and pub["changed"] > now:
            # Whatever the recent-runs policy: a future-dated publication is
            # an ambiguous (possibly new) write, never quarantined.
            raise Problem(REASON_PUB_FUTURE)
        if run_id in ctx.env_blob:
            raise Problem(REASON_ENV)
        info["tree_fp"] = tree["tree_fp"]
        info["pub_fp"] = pub["fp"] if pub else None
        info["has_pub"] = pub is not None
        info["bytes"] = tree["bytes"] + (pub["bytes"] if pub else 0)
        if check_activity:
            paths = [ctx.run_path(run_id, rel) for rel in tree["rels"]]
            if pub is not None:
                paths.append(ctx.pub_path(run_id))
            state = open_state(paths, exempt, ctx.deadline)
            if state == "open":
                raise Problem(REASON_OPEN)
            if state != "closed":
                raise Problem("activity check " + state)
    except Problem as exc:
        info["reason"] = exc.reason
        return info
    info["fingerprint"] = combined_fingerprint(info["tree_fp"], info["pub_fp"], info["mode"])
    return info


def list_run_names(runs_fd):
    """(sorted run-id folder names, complete, other entry count)."""
    names, other, complete = [], 0, True
    with os.scandir(runs_fd) as it:
        for entry in it:
            if not RUN_ID_RE.match(entry.name):
                other += 1
                continue
            if len(names) >= MAX_LISTED_NAMES:
                complete = False
                break
            names.append(entry.name)
    names.sort()
    return names, complete, other


def check_same_filesystem(ctx, quarantine_dev):
    devs = {quarantine_dev}
    for fd in (ctx.runs_fd, ctx.pub_runs_fd):
        if fd is not None:
            devs.add(os.fstat(fd).st_dev)
    if len(devs) != 1:
        raise CleanupError("the run reports, publications and ~/.copilot/copilot-s-report-quarantine are not all on "
                           "the same filesystem; this cleanup only moves with a same-filesystem rename and never "
                           "copies — nothing was changed")


def quarantine_dev_for_preview(home):
    """Filesystem of the quarantine root (or of its nearest existing
    parent) without creating anything."""
    path = os.path.join(home, *QUARANTINE_CHAIN)
    while not os.path.lexists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return os.lstat(path).st_dev
    except OSError:
        raise CleanupError("cannot check the quarantine filesystem")


# --------------------------------------------------------------------------
# Quarantine batches and manifests
# --------------------------------------------------------------------------

def write_file_atomic_at(dir_fd, name, data, mode=0o600):
    """Exclusive temp file in the same directory + fsync + atomic replace +
    directory fsync."""
    tmp = ".%s.%s.tmp" % (name, secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                 dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    fsync_fd(dir_fd)


def save_manifest(batch_fd, manifest):
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MANIFEST_MAX_BYTES:
        raise ManifestError("manifest would exceed %d bytes" % MANIFEST_MAX_BYTES)
    try:
        write_file_atomic_at(batch_fd, MANIFEST_FILE, data)
    except OSError as exc:
        raise ManifestError(exc.strerror or str(exc))


def _optional(value, check):
    return value is None or check(value)


def _valid_entry(entry):
    return (isinstance(entry, dict)
            and isinstance(entry.get("id"), str) and RUN_ID_RE.match(entry["id"]) is not None
            and entry.get("state") in ENTRY_STATES
            and _optional(entry.get("fingerprint"), lambda v: isinstance(v, str) and FINGERPRINT_RE.match(v))
            and _optional(entry.get("tree_fp"), lambda v: isinstance(v, str) and FINGERPRINT_RE.match(v))
            and _optional(entry.get("pub_fp"), lambda v: isinstance(v, str) and FINGERPRINT_RE.match(v))
            and _optional(entry.get("mode"), lambda v: v in MODES)
            and _optional(entry.get("bytes"), lambda v: strict_int(v) and v >= 0)
            and _optional(entry.get("finalized_at"), lambda v: epoch_of_iso(v) is not None)
            and _optional(entry.get("reason"), lambda v: isinstance(v, str) and len(v) <= REASON_MAX_CHARS))


def read_manifest(batch_fd, batch):
    try:
        fd = os.open(MANIFEST_FILE, FILE_READ_FLAGS, dir_fd=batch_fd)
    except OSError as exc:
        raise CleanupError("cannot read the manifest of batch %s (%s)" % (batch, exc.strerror))
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                or st.st_mode & 0o022 or st.st_size > MANIFEST_MAX_BYTES):
            raise CleanupError("the manifest of batch %s is not a private regular file" % batch)
        data = _read_limited(fd, MANIFEST_MAX_BYTES)
    finally:
        os.close(fd)
    if len(data) > MANIFEST_MAX_BYTES:
        raise CleanupError("the manifest of batch %s is too large" % batch)
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise CleanupError("the manifest of batch %s is unreadable" % batch)
    if (not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND
            or not strict_int(manifest.get("version")) or manifest.get("version") != MANIFEST_VERSION
            or manifest.get("batch") != batch or not isinstance(manifest.get("include_recent"), bool)
            or epoch_of_iso(manifest.get("created_at")) is None
            or not isinstance(manifest.get("entries"), list)
            or len(manifest["entries"]) > MAX_BATCH_ENTRIES
            or not all(_valid_entry(entry) for entry in manifest["entries"])
            or len({entry["id"] for entry in manifest["entries"]}) != len(manifest["entries"])):
        raise CleanupError("the manifest of batch %s is not a valid report-cleanup manifest" % batch)
    return manifest


class Batch(object):
    def __init__(self, root_fd, batch_fd, runs_fd, pubs_fd, manifest, name):
        self.root_fd, self.batch_fd, self.runs_fd, self.pubs_fd = root_fd, batch_fd, runs_fd, pubs_fd
        self.manifest, self.name = manifest, name

    def close(self):
        close_fds(self.pubs_fd, self.runs_fd, self.batch_fd, self.root_fd)


def require_private_quarantine(fd, label):
    """The quarantine root/batch must be owner-only (0700-style)."""
    if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        raise CleanupError("%s must be private (chmod 700); refused, not repaired" % label)


def open_batch(batch):
    if not BATCH_RE.match(batch or ""):
        raise CleanupError("invalid batch name %r (expected e.g. 20261119-120000-1a2b3c4d)" % batch)
    root_fd = open_home_chain(QUARANTINE_CHAIN)
    if root_fd is None:
        raise CleanupError("no report quarantine at ~/%s" % "/".join(QUARANTINE_CHAIN))
    batch_fd = runs_fd = pubs_fd = None
    try:
        require_private_quarantine(root_fd, "~/" + "/".join(QUARANTINE_CHAIN))
        label = "~/%s/%s" % ("/".join(QUARANTINE_CHAIN), batch)
        try:
            batch_fd = open_dir_at(root_fd, batch, label)
        except FileNotFoundError:
            raise CleanupError("batch %s not found" % batch)
        require_private_quarantine(batch_fd, label)
        manifest = read_manifest(batch_fd, batch)
        try:
            runs_fd = open_dir_at(batch_fd, BATCH_RUNS_DIR, label + "/" + BATCH_RUNS_DIR)
        except FileNotFoundError:
            runs_fd = None
        try:
            pubs_fd = open_dir_at(batch_fd, BATCH_PUBLICATIONS_DIR, label + "/" + BATCH_PUBLICATIONS_DIR)
        except FileNotFoundError:
            pubs_fd = None
        return Batch(root_fd, batch_fd, runs_fd, pubs_fd, manifest, batch)
    except BaseException:
        close_fds(pubs_fd, runs_fd, batch_fd, root_fd)
        raise


def new_batch_name():
    return "%s-%s" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), secrets.token_hex(4))


def purge_journal_block(root_fd, batch):
    """None, or why `batch` must not be restored: a permanent purge of it was
    started (a journal <quarantine>/.purge/<batch>.json exists, whatever its
    state) or the purge control folder cannot be checked safely (fail
    closed). Deliberately independent of copilot_quarantine_purge.py so a
    restore never depends on that file being importable."""
    try:
        cfd = os.open(PURGE_CONTROL_DIR, DIR_FLAGS | NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return "the purge control folder %s is not a plain folder; refused" % PURGE_CONTROL_DIR
    try:
        st = os.fstat(cfd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return "the purge control folder %s is not a folder owned by you; refused" % PURGE_CONTROL_DIR
        try:
            os.stat(batch + ".json", dir_fd=cfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return "its purge journal cannot be checked; refused"
        return ("a permanent purge of this batch was started (journal %s/%s.json); its data is or may be "
                "partly deleted and it can no longer be restored by this tool — review it yourself"
                % (PURGE_CONTROL_DIR, batch))
    finally:
        os.close(cfd)


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------

SCOPE_LINES = (
    "  Scope: every copilot-s run report in ~/.copilot/session-reports/runs — ALL workspaces and",
    "  directories (run records do not store the working directory, so there is no",
    "  current-directory filter) — each with its private raw/ data (Copilot usage file; for RTK",
    "  launches RTK's own history DB, tee/recall stores) and, for RTK launches, its matching",
    "  publication ~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json.",
    "  Never touched: the global RTK database/config, Copilot's session-state, logs and OTEL",
    "  files, publications without a run report, a custom COPILOT_SESSION_REPORTS_DIR.",
)


def describe(info):
    session = (info.get("session") or "")[:8]
    return "%s  %-6s  %-13s  finalized %s  session %s  %s%s" % (
        info["id"], info.get("mode") or "?", info.get("state") or "?", info.get("finalized_at") or "?",
        (session + "…") if session else "unknown", human_bytes(info.get("bytes") or 0),
        "  +publication" if info.get("has_pub") else "")


def cmd_preview(args):
    ctx = Context(args.include_recent, PREVIEW_TIME_BUDGET_SECONDS)
    candidates, skipped, listing_note = [], Counter(), None
    try:
        ctx.open_sources()
        label = "finalized at least 24h ago" if not ctx.include_recent else "including recently finalized runs"
        print("Run-report cleanup preview (all workspaces; %s)" % label)
        for line in SCOPE_LINES:
            print(line)
        if ctx.runs_fd is None:
            print("")
            print("  No run reports at ~/%s — nothing to clean up." % "/".join(REPORTS_CHAIN))
            print("")
            return 0
        check_same_filesystem(ctx, quarantine_dev_for_preview(ctx.home))
        names, complete, other = list_run_names(ctx.runs_fd)
        if not complete:
            listing_note = "only the first %d run folders were listed" % MAX_LISTED_NAMES
        if other:
            skipped["not a run-report folder name (ignored)"] += other
        eligible = []
        for index, run_id in enumerate(names):
            if time.monotonic() > ctx.deadline:
                skipped[REASON_BUDGET] += len(names) - index
                break
            info = inspect_run(ctx, run_id)
            if info["reason"]:
                skipped[info["reason"]] += 1
            else:
                eligible.append(info)
        eligible.sort(key=lambda item: (item["finalized_at"] or "", item["id"]))
        if len(eligible) > MAX_BATCH_ENTRIES:
            skipped[REASON_OVER_LIMIT] += len(eligible) - MAX_BATCH_ENTRIES
            eligible = eligible[:MAX_BATCH_ENTRIES]
        for info in eligible:
            checked = inspect_run(ctx, info["id"], check_activity=True)
            if checked["reason"]:
                skipped[checked["reason"]] += 1
            elif checked["fingerprint"] != info["fingerprint"]:
                skipped[REASON_CHANGING] += 1
            else:
                candidates.append(checked)
    finally:
        ctx.close()

    print("")
    total = sum(info["bytes"] for info in candidates)
    if candidates:
        print("  %d run report(s) can be moved to quarantine:" % len(candidates))
        for info in candidates:
            print("    - " + describe(info))
        print("")
        print("  Why each qualifies: a finalized launch (completed, copilot-error or interrupted) with a")
        print("  consistent, valid record, plain private files only, not referenced by this environment")
        if ctx.include_recent:
            print("  and not open by any process. Recently finalized runs included: no minimum age")
            print("  (nothing may be in the future). A run finalized moments ago may still be read by a")
            print("  viewer; that is accepted because quarantine is recoverable.")
        else:
            print("  and not open by any process; finalized and last changed more than 24h ago.")
    else:
        print("  No run reports are eligible for quarantine.")
    if skipped:
        print("")
        print("  Kept (not eligible): %d" % sum(skipped.values()))
        for reason, count in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])):
            print("    %4d  %s" % (count, reason))
    if listing_note:
        print("  Note: %s." % listing_note)
    if candidates:
        print("")
        print("  Destination: ~/%s/<new batch>/ (private, same disk)." % "/".join(QUARANTINE_CHAIN))
        print("  Disk space: NOT freed. Quarantine moves (never copies or deletes) the data, so all")
        print("  %s stay on disk until you remove a batch yourself; nothing is purged automatically." %
              human_bytes(total))
        print("  Session reports (copilot-s --session-report, compare) stop counting moved launches")
        print("  until they are restored; each launch is moved on its own — other launches and resumes")
        print("  of the same session are never touched.")
        print("  Restore with: copilot-report-cleanup.py restore --batch <batch> [--yes]")
        marker = "\t" + INCLUDE_RECENT_MARKER if ctx.include_recent else ""
        for info in candidates:
            print("%s\t%s\t%s%s" % (CANDIDATE_TAG, info["id"], info["fingerprint"], marker))
    print("")
    return 0


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def read_requested(stream, include_recent):
    """Parse the exact preview lines
    "CANDIDATE<TAB><id><TAB><fingerprint>[<TAB>include-recent]" (the only
    accepted format); any policy mismatch, duplicate or malformed line
    rejects the whole request."""
    raw = stream.read(STDIN_MAX_BYTES + 1)
    if len(raw) > STDIN_MAX_BYTES:
        raise CleanupError("too much input on stdin (nothing was moved)")
    requested, seen = [], set()
    for line in raw.split("\n"):
        if line == "":
            continue
        parts = line.split("\t")
        if not parts or parts[0] != CANDIDATE_TAG:
            raise CleanupError("malformed candidate line on stdin: %r (nothing was moved)" % line[:200])
        parts = parts[1:]
        line_recent = len(parts) == 3 and parts[2] == INCLUDE_RECENT_MARKER
        if line_recent:
            parts = parts[:2]
        if len(parts) != 2 or not RUN_ID_RE.match(parts[0]) or not FINGERPRINT_RE.match(parts[1]):
            raise CleanupError("malformed candidate line on stdin: %r (nothing was moved)" % line[:200])
        if line_recent != include_recent:
            raise CleanupError("candidate %s was previewed %s --include-recent but apply was run %s it; preview "
                               "and apply must use the same option (nothing was moved)"
                               % (parts[0], "with" if line_recent else "without",
                                  "with" if include_recent else "without"))
        if parts[0] in seen:
            raise CleanupError("duplicate candidate %s on stdin (nothing was moved)" % parts[0])
        seen.add(parts[0])
        requested.append((parts[0], parts[1]))
    if len(requested) > MAX_BATCH_ENTRIES:
        raise CleanupError("at most %d runs per batch (nothing was moved)" % MAX_BATCH_ENTRIES)
    return requested


def _set(entry, state, reason=None):
    entry["state"] = state
    entry["reason"] = reason[:REASON_MAX_CHARS] if isinstance(reason, str) else None


def _rollback(ctx, batch, run_id, run_moved, pub_moved):
    """Move what was moved back (run folder first, so a publication never
    returns without its run). Returns a list of problems (empty = done)."""
    problems = []
    name = run_id + PUBLICATION_SUFFIX
    if run_moved:
        try:
            rename_noreplace_at(batch.runs_fd, run_id, ctx.runs_fd, run_id)
            run_moved = False
        except OSError as exc:
            problems.append("run folder could not be moved back (%s)" % move_error_text(exc))
    if pub_moved and not run_moved:
        try:
            rename_noreplace_at(batch.pubs_fd, name, ctx.pub_runs_fd, name)
        except OSError as exc:
            problems.append("publication could not be moved back (%s)" % move_error_text(exc))
    elif pub_moved:
        problems.append("publication kept in quarantine with its run")
    for fd in (ctx.runs_fd, ctx.pub_runs_fd, batch.runs_fd, batch.pubs_fd):
        fsync_fd(fd)
    return problems


def quarantine_one(ctx, batch, entry):
    """Re-validate and move one previewed run. Lock order: (transaction,
    reconcile held by the caller) -> run lock -> capacity lock. Manifest
    transitions, each saved durably:
      requested -> skipped
      requested -> pending (fingerprints recorded) [-> pub_moved] -> moved
                -> skipped (moved back) | failed (left for `restore`)
    Raises ManifestError (after rolling back) when the manifest cannot be
    saved; the caller stops."""
    run_id = entry["id"]
    manifest = batch.manifest
    try:
        lock_fd = acquire_run_lock(ctx.runs_fd, run_id)
    except Problem as exc:
        _set(entry, "skipped", exc.reason)
        save_manifest(batch.batch_fd, manifest)
        return
    if lock_fd is None:
        _set(entry, "skipped", REASON_BUSY)
        save_manifest(batch.batch_fd, manifest)
        return
    cap_fd = None
    try:
        info = inspect_run(ctx, run_id, check_activity=True, exempt=frozenset([own_lock_fact(lock_fd)]))
        if info["reason"] or info["fingerprint"] != entry["fingerprint"]:
            _set(entry, "skipped", info["reason"] or REASON_CHANGED)
            save_manifest(batch.batch_fd, manifest)
            return
        if ctx.export_fd is not None:
            try:
                cap_fd = acquire_lock(ctx.export_fd, CAPACITY_LOCK_NAME, CAPACITY_LOCK_WAIT_SECONDS)
            except Problem as exc:
                _set(entry, "skipped", exc.reason)
                save_manifest(batch.batch_fd, manifest)
                return
            if cap_fd is None:
                _set(entry, "skipped", REASON_PUB_BUSY)
                save_manifest(batch.batch_fd, manifest)
                return
        # Final re-validation under every lock (no producer can change the
        # run or its publication from here on).
        info = inspect_run(ctx, run_id)
        if info["reason"] or info["fingerprint"] != entry["fingerprint"]:
            _set(entry, "skipped", info["reason"] or REASON_CHANGED)
            save_manifest(batch.batch_fd, manifest)
            return
        entry.update({"mode": info["mode"], "tree_fp": info["tree_fp"], "pub_fp": info["pub_fp"],
                      "bytes": info["bytes"], "finalized_at": info["finalized_at"]})
        _set(entry, "pending")
        save_manifest(batch.batch_fd, manifest)
        run_moved = pub_moved = False
        name = run_id + PUBLICATION_SUFFIX
        try:
            if info["has_pub"]:
                try:
                    rename_noreplace_at(ctx.pub_runs_fd, name, batch.pubs_fd, name)
                except OSError as exc:
                    _set(entry, "skipped", "publication not moved: %s" % move_error_text(exc))
                    save_manifest(batch.batch_fd, manifest)
                    return
                pub_moved = True
                fsync_fd(ctx.pub_runs_fd)
                fsync_fd(batch.pubs_fd)
                _set(entry, "pub_moved")
                save_manifest(batch.batch_fd, manifest)
            try:
                rename_noreplace_at(ctx.runs_fd, run_id, batch.runs_fd, run_id)
            except OSError as exc:
                problems = _rollback(ctx, batch, run_id, False, pub_moved)
                pub_moved = False
                _set(entry, "failed" if problems else "skipped",
                     "run folder not moved: %s%s" % (move_error_text(exc),
                                                     ("; " + "; ".join(problems)) if problems else ""))
                save_manifest(batch.batch_fd, manifest)
                return
            run_moved = True
            fsync_fd(ctx.runs_fd)
            fsync_fd(batch.runs_fd)
            # Verify what arrived is exactly what was validated.
            try:
                moved_tree = scan_run_tree(ctx, batch.runs_fd, run_id)["tree_fp"]
                moved_pub = read_publication(ctx, batch.pubs_fd, run_id) if pub_moved else None
            except Problem:
                moved_tree, moved_pub = None, None
            if moved_tree != entry["tree_fp"] or (pub_moved and (moved_pub or {}).get("fp") != entry["pub_fp"]):
                problems = _rollback(ctx, batch, run_id, run_moved, pub_moved)
                if problems:
                    _set(entry, "failed", "changed while moving and not fully moved back (%s); left for "
                                          "`restore` / manual review" % "; ".join(problems))
                else:
                    _set(entry, "skipped", "changed while moving; moved back")
                run_moved = pub_moved = False
                save_manifest(batch.batch_fd, manifest)
                return
            _set(entry, "moved")
            save_manifest(batch.batch_fd, manifest)
        except ManifestError:
            if run_moved or pub_moved:
                _rollback(ctx, batch, run_id, run_moved, pub_moved)
            raise
    finally:
        close_fds(cap_fd, lock_fd)


def cmd_apply(args):
    requested = read_requested(sys.stdin, args.include_recent)
    if not requested:
        print("Nothing to quarantine.")
        return 0
    _no_replace_rename_func()  # fail before creating anything if unsupported
    ctx = Context(args.include_recent, APPLY_TIME_BUDGET_SECONDS)
    root_fd = txn_fd = reconcile_fd = None
    batch = None
    entries = [{"id": run_id, "fingerprint": fp, "state": "requested", "reason": None, "mode": None,
                "tree_fp": None, "pub_fp": None, "bytes": None, "finalized_at": None} for run_id, fp in requested]
    aborted = None
    try:
        ctx.open_sources()
        if ctx.runs_fd is None:
            raise CleanupError("no run reports at ~/%s; nothing was changed" % "/".join(REPORTS_CHAIN))
        # Read-only pre-pass (no locks yet): which previewed runs are still
        # unchanged, and whether any is an RTK run (needs the publication
        # folder's locks even when that folder does not exist yet).
        need_export = False
        for entry in entries:
            info = inspect_run(ctx, entry["id"])
            if info["reason"] or info["fingerprint"] != entry["fingerprint"]:
                _set(entry, "skipped", info["reason"] or REASON_CHANGED)
            elif info["mode"] == "rtk":
                need_export = True
        if not any(entry["state"] == "requested" for entry in entries):
            print("")
            print("Nothing was moved and no quarantine batch was created: no previewed run is still eligible.")
            for entry in entries:
                print("    skipped %s: %s" % (entry["id"], entry["reason"]))
            return 0
        root_fd = open_home_chain(QUARANTINE_CHAIN, create=True)
        require_private_quarantine(root_fd, "~/" + "/".join(QUARANTINE_CHAIN))
        txn_fd = acquire_global_lock(root_fd, TRANSACTION_LOCK_NAME, TRANSACTION_LOCK_WAIT_SECONDS,
                                     "the report quarantine")
        if need_export:
            ctx.open_export(create=True)
        check_same_filesystem(ctx, os.fstat(root_fd).st_dev)
        if ctx.export_fd is not None:
            reconcile_fd = acquire_global_lock(ctx.export_fd, RECONCILE_LOCK_NAME, RECONCILE_LOCK_WAIT_SECONDS,
                                               "RTK publication reconciliation")
        name = new_batch_name()
        label = "~/%s/%s" % ("/".join(QUARANTINE_CHAIN), name)
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
            batch_fd = open_dir_at(root_fd, name, label)
            batch = Batch(None, batch_fd, None, None, None, name)
            os.mkdir(BATCH_RUNS_DIR, 0o700, dir_fd=batch_fd)
            batch.runs_fd = open_dir_at(batch_fd, BATCH_RUNS_DIR, label + "/" + BATCH_RUNS_DIR)
            os.mkdir(BATCH_PUBLICATIONS_DIR, 0o700, dir_fd=batch_fd)
            batch.pubs_fd = open_dir_at(batch_fd, BATCH_PUBLICATIONS_DIR, label + "/" + BATCH_PUBLICATIONS_DIR)
        except OSError as exc:
            raise CleanupError("cannot create quarantine batch %s: %s (nothing was moved)" % (label, exc.strerror))
        batch.manifest = {"kind": MANIFEST_KIND, "version": MANIFEST_VERSION, "batch": name,
                          "created_at": utc_now_iso(), "include_recent": ctx.include_recent, "entries": entries}
        try:
            save_manifest(batch.batch_fd, batch.manifest)
        except ManifestError as exc:
            raise CleanupError("cannot write the manifest in %s: %s (nothing was moved)" % (label, exc))
        for entry in entries:
            if entry["state"] != "requested":
                continue
            if time.monotonic() > ctx.deadline:
                _set(entry, "skipped", REASON_BUDGET)
                continue
            try:
                quarantine_one(ctx, batch, entry)
            except ManifestError as exc:
                aborted = "manifest update failed (%s); stopped before further moves" % exc
                break
        if aborted is None:
            try:
                save_manifest(batch.batch_fd, batch.manifest)
            except ManifestError:
                pass
    finally:
        if batch is not None:
            batch.close()
        close_fds(reconcile_fd, txn_fd, root_fd)
        ctx.close()

    counts = Counter(entry["state"] for entry in entries)
    moved_bytes = sum(entry["bytes"] or 0 for entry in entries if entry["state"] == "moved")
    print("")
    print("Report quarantine batch: %s" % batch.name)
    print("  Location: ~/%s/%s" % ("/".join(QUARANTINE_CHAIN), batch.name))
    if ctx.include_recent:
        print("  Recently finalized runs included, as previewed.")
    print("  Moved: %d   Skipped: %d   Failed: %d" % (counts["moved"], counts["skipped"], counts["failed"]))
    for entry in entries:
        if entry["state"] in ("skipped", "failed"):
            print("    %s %s: %s" % (entry["state"], entry["id"], entry["reason"]))
        elif entry["state"] not in ("moved",):
            print("    not completed %s (state %s)" % (entry["id"], entry["state"]))
    if counts["moved"]:
        print("  Disk space: NOT freed — %s stay on disk in the quarantine batch." % human_bytes(moved_bytes))
    if aborted:
        print("  Error: %s" % aborted)
        print("  The manifest records pending moves; `restore` reconciles them.")
    if counts["moved"] or counts["failed"] or aborted:
        print("  Restore with: copilot-report-cleanup.py restore --batch %s --yes" % batch.name)
    return 1 if (counts["failed"] or aborted) else 0


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def cmd_list(args):
    root_fd = open_home_chain(QUARANTINE_CHAIN)
    location = "~/" + "/".join(QUARANTINE_CHAIN)
    if root_fd is None:
        print("No report quarantine batches (%s does not exist)." % location)
        return 0
    names, truncated = [], False
    try:
        with os.scandir(root_fd) as it:
            for entry in it:
                if BATCH_RE.match(entry.name):
                    if len(names) >= MAX_LISTED_BATCHES:
                        truncated = True
                        break
                    names.append(entry.name)
        purge_notes = {name: purge_journal_block(root_fd, name) for name in names}
    finally:
        os.close(root_fd)
    names.sort()
    if not names:
        print("No report quarantine batches in %s." % location)
        return 0
    print("Report quarantine batches in %s (data stays on disk until you remove a batch):" % location)
    for name in names:
        # Additive status only; lines and BATCH machine lines are unchanged.
        purge_note = "  [permanent purge started: not restorable]" if purge_notes.get(name) else ""
        try:
            batch = open_batch(name)
        except CleanupError as exc:
            print("  %s  (unreadable: %s)%s" % (name, exc, purge_note))
            continue
        batch.close()
        entries = batch.manifest["entries"]
        states = Counter(entry["state"] for entry in entries)
        size = sum(entry.get("bytes") or 0 for entry in entries if entry["state"] in ("moved", "pub_moved",
                                                                                          "pending", "failed"))
        summary = ", ".join("%s %d" % (state, count) for state, count in sorted(states.items()))
        print("  %s  created %s  %s  (%s)%s%s" % (name, batch.manifest["created_at"], summary, human_bytes(size),
                                                 "  [recent included]" if batch.manifest["include_recent"] else "",
                                                 purge_note))
    if truncated:
        print("  Note: only the first %d batches are listed." % MAX_LISTED_BATCHES)
    print("Restore plan (read-only): copilot-report-cleanup.py restore --batch <batch>")
    print("Restore:                  copilot-report-cleanup.py restore --batch <batch> --yes")
    for name in names:
        print("BATCH\t%s" % name)
    return 0


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def plan_restore_entry(ctx, batch, entry):
    """(action, detail): action None (nothing in quarantine), "refuse",
    "restore" (run folder, then its publication if quarantined) or
    "restore-publication" (an interrupted apply left the run in place).
    Locations come only from the batch layout and the canonical paths of
    the validated run id; nothing is merged or overwritten."""
    run_id = entry["id"]
    name = run_id + PUBLICATION_SUFFIX
    run_q = batch.runs_fd is not None and lexists_at(batch.runs_fd, run_id)
    pub_q = batch.pubs_fd is not None and lexists_at(batch.pubs_fd, name)
    if not run_q and not pub_q:
        return None, None
    if (run_q and not entry.get("tree_fp")) or (pub_q and not entry.get("pub_fp")):
        return "refuse", "no fingerprint recorded for the quarantined data; review it manually"
    run_c = ctx.runs_fd is not None and lexists_at(ctx.runs_fd, run_id)
    pub_c = ctx.pub_runs_fd is not None and lexists_at(ctx.pub_runs_fd, name)
    if run_q:
        if run_c:
            return "refuse", "a run report with this id already exists at the original place"
        try:
            tree = scan_run_tree(ctx, batch.runs_fd, run_id)
        except Problem as exc:
            return "refuse", "the quarantined run folder is not safe to restore (%s)" % exc.reason
        if tree["tree_fp"] != entry["tree_fp"]:
            return "refuse", "the quarantined run folder changed; restore it manually if intended"
    elif not run_c:
        return "refuse", "the run folder is neither in quarantine nor at its original place"
    if pub_q:
        if pub_c:
            return "refuse", "a publication with this id already exists at the original place"
        try:
            pub = read_publication(ctx, batch.pubs_fd, run_id)
        except Problem as exc:
            return "refuse", "the quarantined publication is not safe to restore (%s)" % exc.reason
        if pub is None or pub["fp"] != entry["pub_fp"]:
            return "refuse", "the quarantined publication changed; restore it manually if intended"
    if run_q:
        return "restore", "run report%s" % (" + publication" if pub_q else "")
    return "restore-publication", "publication only (its run report was never moved)"


def count_publications(pub_runs_fd):
    count = 0
    with os.scandir(pub_runs_fd) as it:
        for scanned, entry in enumerate(it):
            if scanned >= MAX_PUBLICATION_COUNT_SCAN:
                return PUBLICATION_MAX_FILES  # unknown: treat as full (fail closed)
            name = entry.name
            if name.endswith(PUBLICATION_SUFFIX) and RUN_ID_RE.match(name[:-len(PUBLICATION_SUFFIX)]):
                count += 1
    return count


def restore_one(ctx, batch, entry):
    """Restore one planned entry under: (transaction, reconcile held by the
    caller) -> run lock -> capacity lock. Returns (ok, message)."""
    run_id = entry["id"]
    manifest = batch.manifest
    name = run_id + PUBLICATION_SUFFIX
    lock_parent = batch.runs_fd if (batch.runs_fd is not None and lexists_at(batch.runs_fd, run_id)) \
        else ctx.runs_fd
    if lock_parent is None:
        return False, "the run folder is neither in quarantine nor at its original place"
    try:
        lock_fd = acquire_run_lock(lock_parent, run_id)
    except Problem as exc:
        return False, exc.reason
    if lock_fd is None:
        return False, REASON_BUSY
    cap_fd = None
    try:
        if ctx.export_fd is not None:
            try:
                cap_fd = acquire_lock(ctx.export_fd, CAPACITY_LOCK_NAME, CAPACITY_LOCK_WAIT_SECONDS)
            except Problem as exc:
                return False, exc.reason
            if cap_fd is None:
                return False, REASON_PUB_BUSY
        action, detail = plan_restore_entry(ctx, batch, entry)
        if action is None:
            return False, "nothing left in quarantine"
        if action == "refuse":
            return False, detail
        pub_q = batch.pubs_fd is not None and lexists_at(batch.pubs_fd, name)
        if action == "restore":
            previous = entry["state"]
            _set(entry, "restoring")
            save_manifest(batch.batch_fd, manifest)
            try:
                rename_noreplace_at(batch.runs_fd, run_id, ctx.runs_fd, run_id)
            except OSError as exc:
                _set(entry, previous, entry.get("reason"))
                save_manifest(batch.batch_fd, manifest)
                return False, "not restored: %s" % move_error_text(exc)
            fsync_fd(batch.runs_fd)
            fsync_fd(ctx.runs_fd)
            _set(entry, "run_restored")
            save_manifest(batch.batch_fd, manifest)
        if pub_q:
            if ctx.pub_runs_fd is None:
                return action == "restore", "run report restored; publication left in quarantine (no folder)"
            if count_publications(ctx.pub_runs_fd) >= PUBLICATION_MAX_FILES:
                _set(entry, entry["state"], "publication left in quarantine (publication folder full)")
                save_manifest(batch.batch_fd, manifest)
                return action == "restore", "publication left in quarantine (publication folder full; retry later)"
            try:
                rename_noreplace_at(batch.pubs_fd, name, ctx.pub_runs_fd, name)
            except OSError as exc:
                _set(entry, entry["state"], "publication not restored: %s" % move_error_text(exc))
                save_manifest(batch.batch_fd, manifest)
                return action == "restore", "publication not restored: %s" % move_error_text(exc)
            fsync_fd(batch.pubs_fd)
            fsync_fd(ctx.pub_runs_fd)
        _set(entry, "restored")
        save_manifest(batch.batch_fd, manifest)
        return True, detail
    finally:
        close_fds(cap_fd, lock_fd)


def cmd_restore(args):
    ctx = Context(False, APPLY_TIME_BUDGET_SECONDS)
    batch = None
    root_fd = txn_fd = reconcile_fd = None
    try:
        # Restoring always targets the default canonical locations the
        # batch came from; a custom reports root does not redirect it.
        ctx.open_sources(check_root=False)
        batch = open_batch(args.batch)
        blocked = purge_journal_block(batch.root_fd, batch.name)
        if blocked:
            raise CleanupError("batch %s: %s" % (batch.name, blocked))
        plans = [(entry,) + plan_restore_entry(ctx, batch, entry) for entry in batch.manifest["entries"]]
        todo = [(entry, action, detail) for entry, action, detail in plans if action not in (None, "refuse")]
        refused = [(entry, detail) for entry, action, detail in plans if action == "refuse"]
        print("Restore plan for report quarantine batch %s:" % batch.name)
        for entry, action, detail in todo:
            print("  will restore %s (%s)" % (entry["id"], detail))
        for entry, detail in refused:
            print("  refused %s: %s" % (entry["id"], detail))
        if not todo:
            print("  Nothing to restore.")
        if not args.yes:
            if todo:
                print("  Nothing is overwritten or merged; restore with --yes (re-validated under locks).")
            print("RESTORABLE\t%d" % len(todo))
            return 0
        if not todo:
            return 1 if refused else 0
        _no_replace_rename_func()
        root_fd = batch.root_fd
        txn_fd = acquire_global_lock(root_fd, TRANSACTION_LOCK_NAME, TRANSACTION_LOCK_WAIT_SECONDS,
                                     "the report quarantine")
        # A purge holds the same transaction lock for its whole destructive
        # phase; re-check its journal now that no purge can be running.
        blocked = purge_journal_block(root_fd, batch.name)
        if blocked:
            raise CleanupError("batch %s: %s (nothing was restored)" % (batch.name, blocked))
        ctx.open_runs(create=any(action == "restore" for _, action, _ in todo))
        needs_pub = batch.pubs_fd is not None and any(
            lexists_at(batch.pubs_fd, entry["id"] + PUBLICATION_SUFFIX) for entry, _, _ in todo)
        if needs_pub:
            ctx.open_export(create=True)
        check_same_filesystem(ctx, os.fstat(batch.batch_fd).st_dev)
        if ctx.export_fd is not None:
            reconcile_fd = acquire_global_lock(ctx.export_fd, RECONCILE_LOCK_NAME, RECONCILE_LOCK_WAIT_SECONDS,
                                               "RTK publication reconciliation")
        restored, failures = 0, []
        for entry, _action, _detail in todo:
            try:
                ok, message = restore_one(ctx, batch, entry)
            except ManifestError as exc:
                failures.append((entry["id"], "manifest update failed (%s); stopped" % exc))
                break
            if ok and entry["state"] == "restored":
                restored += 1
            else:
                failures.append((entry["id"], message))
        print("Restored: %d   Not restored: %d" % (restored, len(failures)))
        for run_id, why in failures:
            print("  %s: %s" % (run_id, why))
        return 1 if (failures or refused) else 0
    finally:
        close_fds(reconcile_fd, txn_fd)
        if batch is not None:
            batch.close()
        ctx.close()


# --------------------------------------------------------------------------
# Permanent purge of one batch (engine: copilot_quarantine_purge.py)
# --------------------------------------------------------------------------

def load_purge_engine():
    """Load copilot_quarantine_purge.py from this file's REAL directory (the
    repo checkout for symlink installs, the install prefix for --copy
    installs) without writing bytecode next to it."""
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), PURGE_ENGINE_FILE)
    if not os.path.isfile(path):
        raise CleanupError("purge support file %s is missing; re-run the installer (nothing was changed)" % path)
    import importlib.util
    spec = importlib.util.spec_from_file_location("copilot_quarantine_purge", path)
    if spec is None or spec.loader is None:
        raise CleanupError("cannot load %s (nothing was changed)" % path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CleanupError("cannot load %s (%s); nothing was changed" % (path, exc))
    finally:
        sys.dont_write_bytecode = previous
    return module


class ReportPurgeAdapter(object):
    """Report-quarantine side of copilot_quarantine_purge.py: strict
    purge-only manifest validation on top of read_manifest, run/publication
    pairing, the recorded tree/publication fingerprints (scan_run_tree and
    read_publication, the semantics `restore` uses), exact batch layout, and
    the quarantine transaction lock. Paths come only from validated run ids.
    Never opens or creates anything at the canonical report/publication
    locations."""

    kind = "reports"
    title = "quarantined run reports"
    prog = "copilot-report-cleanup.py"

    def __init__(self, engine):
        self.engine = engine
        self.root_path = os.path.join(home_dir(), *QUARANTINE_CHAIN)

    def open_root(self):
        try:
            fd = open_home_chain(QUARANTINE_CHAIN)
            if fd is None:
                return None
            try:
                require_private_quarantine(fd, "~/" + "/".join(QUARANTINE_CHAIN))
            except BaseException:
                os.close(fd)
                raise
            return fd
        except CleanupError as exc:
            raise self.engine.PurgeError(str(exc))

    def _open(self, batch):
        try:
            return open_batch(batch)
        except CleanupError as exc:
            raise self.engine.Refused(str(exc))

    def _settled(self, manifest):
        """(moved entries, skipped ids) of a strictly valid, fully settled
        manifest (read_manifest already validated every entry's types and
        unique ids); raises Refused otherwise."""
        refused = self.engine.Refused
        if set(manifest) - PURGE_MANIFEST_KEYS:
            raise refused("the manifest has unknown fields")
        if not manifest["entries"]:
            raise refused("the manifest has no entries")
        moved, skipped = [], []
        for entry in manifest["entries"]:
            run_id, state = entry["id"], entry["state"]
            if set(entry) - PURGE_ENTRY_KEYS:
                raise refused("a manifest entry has unknown fields")
            if state == "moved":
                tree_fp, pub_fp, mode = entry.get("tree_fp"), entry.get("pub_fp"), entry.get("mode")
                if not tree_fp or mode not in MODES or not entry.get("fingerprint"):
                    raise refused("entry %s has no recorded fingerprints" % run_id)
                if mode != "rtk" and pub_fp is not None:
                    raise refused("entry %s records a publication for a normal-mode run" % run_id)
                if entry["fingerprint"] != combined_fingerprint(tree_fp, pub_fp, mode):
                    raise refused("entry %s: its recorded fingerprints are inconsistent" % run_id)
                moved.append(entry)
            elif state == "skipped":
                skipped.append(run_id)
            else:
                raise refused("entry %s is %r — only fully settled batches (every entry moved or skipped) can be "
                              "purged" % (run_id, state))
        if not moved:
            raise refused("nothing was moved into this batch")
        return moved, skipped

    def manifest_status(self, root_fd, batch):
        try:
            opened = self._open(batch)
        except self.engine.Refused as exc:
            return False, str(exc)
        try:
            moved, skipped = self._settled(opened.manifest)
        except self.engine.Refused as exc:
            return False, str(exc)
        finally:
            opened.close()
        recorded = sum(entry.get("bytes") or 0 for entry in moved)
        return True, "%d moved run(s), %d skipped, %s recorded" % (len(moved), len(skipped), human_bytes(recorded))

    def verify(self, root_fd, batch, inventory, deadline):
        refused = self.engine.Refused
        opened = self._open(batch)
        try:
            manifest = opened.manifest
            moved, skipped = self._settled(manifest)
            if opened.runs_fd is None or opened.pubs_fd is None:
                raise refused("the batch has no %s/ or %s/ folder" % (BATCH_RUNS_DIR, BATCH_PUBLICATIONS_DIR))
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise refused("not fully inspected (time budget reached; try again)")
            ctx = Context(False, remaining)
            expected = {(MANIFEST_FILE,): "f", (BATCH_RUNS_DIR,): "d", (BATCH_PUBLICATIONS_DIR,): "d"}
            items = []
            for entry in moved:
                run_id = entry["id"]
                pub_name = run_id + PUBLICATION_SUFFIX
                try:
                    tree = scan_run_tree(ctx, opened.runs_fd, run_id)
                except Problem as exc:
                    raise refused("quarantined run %s cannot be verified (%s)" % (run_id, exc.reason))
                if tree["tree_fp"] != entry["tree_fp"]:
                    raise refused("quarantined run %s changed since it was quarantined" % run_id)
                rels = [(BATCH_RUNS_DIR, run_id)]
                expected[(BATCH_RUNS_DIR, run_id)] = "d"
                if entry.get("pub_fp"):
                    try:
                        pub = read_publication(ctx, opened.pubs_fd, run_id)
                    except Problem as exc:
                        raise refused("the quarantined publication of run %s cannot be verified (%s)"
                                      % (run_id, exc.reason))
                    if pub is None:
                        raise refused("the quarantined publication of run %s is missing" % run_id)
                    if pub["fp"] != entry["pub_fp"]:
                        raise refused("the quarantined publication of run %s changed since it was quarantined"
                                      % run_id)
                    expected[(BATCH_PUBLICATIONS_DIR, pub_name)] = "f"
                    rels.append((BATCH_PUBLICATIONS_DIR, pub_name))
                elif lexists_at(opened.pubs_fd, pub_name):
                    raise refused("an unrecorded publication of run %s is in the batch" % run_id)
                items.append((rels, "run %s  %s  finalized %s%s" % (
                    run_id, entry["mode"], entry.get("finalized_at") or "?",
                    "  +publication" if entry.get("pub_fp") else "")))
            for run_id in skipped:
                if lexists_at(opened.runs_fd, run_id) or lexists_at(opened.pubs_fd, run_id + PUBLICATION_SUFFIX):
                    raise refused("skipped run %s has data in the batch" % run_id)
            bst = os.fstat(opened.batch_fd)
        finally:
            opened.close()
        semantic = {
            "manifest_sha256": sha256_json(manifest),
            "moved": [[entry["id"], entry["fingerprint"], entry["tree_fp"], entry.get("pub_fp")] for entry in moved],
            "skipped": sorted(skipped),
        }
        return self.engine.Verification((bst.st_dev, bst.st_ino), expected, (MANIFEST_FILE,), items, semantic,
                                        len(moved))

    @contextlib.contextmanager
    def lock(self, root_fd):
        """The quarantine .transaction.lock (held by apply and restore for
        their whole run), held for the whole destructive phase."""
        try:
            fd = acquire_global_lock(root_fd, TRANSACTION_LOCK_NAME, TRANSACTION_LOCK_WAIT_SECONDS,
                                     "the report quarantine")
        except CleanupError as exc:
            raise self.engine.PurgeError(str(exc))
        try:
            yield
        finally:
            close_fds(fd)


def cmd_purge(args):
    engine = load_purge_engine()
    return engine.run(ReportPurgeAdapter(engine), args.command, getattr(args, "batch", None),
                      getattr(args, "snapshot", None))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="copilot-report-cleanup.py",
        description=("Recoverable quarantine of finalized copilot-s run reports, their private raw data and "
                     "matching RTK publications (used by copilot-s menu r). preview/apply/list/restore never "
                     "delete and free no disk space; purge-list/purge-preview/purge permanently delete ONE "
                     "settled batch only after an exact phrase typed on the terminal."))
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (("preview", "list eligible run reports (read-only)"),
                            ("apply", "quarantine the previewed candidates read from stdin")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--include-recent", action="store_true",
                         help="explicit opt-in: no 24h minimum age after finalization (nothing may be in the "
                              "future); preview and apply must both use it or both omit it")
    sub.add_parser("list", help="list quarantine batches (read-only)")
    restore = sub.add_parser("restore", help="show (read-only) or, with --yes, perform a batch restore")
    restore.add_argument("--batch", required=True)
    restore.add_argument("--yes", action="store_true", help="perform the restore (otherwise only the plan)")
    sub.add_parser("purge-list", help="list quarantine batches and whether each can be purged permanently "
                                      "(read-only)")
    purge_preview = sub.add_parser("purge-preview", help="read-only inventory and estimated reclaimable space "
                                                         "of ONE batch")
    purge_preview.add_argument("--batch", required=True)
    purge = sub.add_parser("purge", help="PERMANENTLY delete ONE fully settled batch after the exact phrase is "
                                         "typed on the terminal (irreversible)")
    purge.add_argument("--batch", required=True)
    purge.add_argument("--snapshot", help="optional: the snapshot printed by purge-preview; refuses if the batch "
                                          "is no longer exactly that snapshot")
    return parser


def main(argv=None):
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {"preview": cmd_preview, "apply": cmd_apply, "list": cmd_list, "restore": cmd_restore,
                "purge-list": cmd_purge, "purge-preview": cmd_purge, "purge": cmd_purge}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command](args)
    except CleanupError as exc:
        print("copilot-report-cleanup: error: %s" % exc, file=sys.stderr)
        return 1
    except Problem as exc:
        print("copilot-report-cleanup: error: %s" % exc.reason, file=sys.stderr)
        return 1
    except OSError as exc:
        print("copilot-report-cleanup: error: %s" % (exc.strerror or exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
