#!/usr/bin/env python3
"""copilot-session-cleanup.py — recoverable quarantine of empty Copilot CLI
sessions, used by `copilot-s` (menu option `c`).

Nothing is ever deleted. Eligible session directories are *renamed* (same
filesystem only, never copied, and never over an existing path) from
~/.copilot/session-state/<id>/ into a private batch directory under
~/.copilot-s-quarantine/<batch>/sessions/<id>/, with a durable manifest that
records every transition so that a batch can be restored later — even after
a crash part-way through.

Subcommands (stdlib only):
  preview  Read-only. Lists eligible empty sessions for one exact cwd (or
           --all). Human-readable lines go to stdout; each candidate is also
           emitted as a machine line "CANDIDATE<TAB><id><TAB><fingerprint>"
           that copilot-s passes back to `apply` after confirmation.
  apply    Reads exactly those "<id><TAB><fingerprint>" lines on stdin. For
           each one: re-checks eligibility and activity, then — holding the
           ~/.copilot-sessions lock — re-reads the session list, re-checks
           eligibility and the preview fingerprint, durably records the
           session's list rows in the manifest, moves it, and removes only
           its rows. Never quarantines anything that was not in the preview.
  list     Lists quarantine batches and their entry states.
  restore  `restore --batch <batch>`: shows what will be restored and asks
           for exactly "yes". Refuses symlinked/foreign quarantine paths, ID
           collisions (atomically, via a no-replace rename), and entries
           whose quarantined contents changed. Re-adds a recorded
           ~/.copilot-sessions row only if no row for that ID exists.

Usage (path options, if given, go before the subcommand; defaults are
~/.copilot/session-state, ~/.copilot-sessions, ~/.copilot-s-quarantine):
  copilot-session-cleanup.py [--session-state-dir D] [--sessions-file F]
      [--quarantine-dir Q] {preview|apply} (--cwd DIR | --all)
      [--include-recent]
  copilot-session-cleanup.py list
  copilot-session-cleanup.py restore --batch <batch>

A session is eligible only if ALL of the following hold (fail closed on
anything unexpected):
  - its directory name is an exact lowercase UUID, it is a real directory
    (not a symlink) owned by the current user, directly in session-state;
  - it contains exactly one entry, a regular single-link file named
    workspace.yaml — or exactly that plus the complete, untouched empty
    scaffold some Copilot CLI versions create for a never-used session:
    an empty single-link regular .workspace-fork.lock, empty files/ and
    research/ directories, and checkpoints/ holding only index.md whose
    bytes are exactly the empty "# Checkpoint History" table header (no
    checkpoint rows). Every scaffold item must be owned by you and is
    inspected through descriptors opened without following symlinks; a
    partial scaffold, any symlink, extra entry, non-empty lock, different
    index.md, or any events.jsonl/session.db/other artifact (even empty)
    excludes the session;
  - workspace.yaml is small, plain `key: value` scalars only, with only
    known keys; id matches the directory; cwd is an absolute normalized path;
    `user_named: false` is explicitly present; there is no name/summary;
    summary_count/fork_count (if present) are 0; created_at/updated_at are
    valid UTC timestamps no more than 1 second apart;
  - it was created at least 24h ago, and the directory, workspace.yaml, and
    every scaffold item were not modified/changed within the last 24h
    (with the explicit opt-in --include-recent this minimum age is 0: those
    same timestamps must instead all be at or before the current time — a
    future timestamp still excludes the session; every other check below is
    unchanged);
  - its cwd matches the requested cwd exactly (unless --all);
  - it has no copilot-s alias in ~/.copilot-sessions (only the generated
    "unnamed" / "<cwd basename> session" names are allowed);
  - its ID does not appear in any environment variable value;
  - a supported open-file check (`lsof`, macOS/Linux) positively reports that
    no process has the directory, workspace.yaml, or any scaffold item (e.g.
    .workspace-fork.lock) open. Unavailable, failing, timed-out, or ambiguous
    checks exclude the session.

The preview fingerprint covers the directory's inode and entry names,
workspace.yaml (inode, size, times, SHA-256) and, for a scaffold session,
every scaffold item (type, inode, mode, owner, times; files also link count,
size, SHA-256). It is re-checked before the move, under the session-list
lock, and right after the move (a mismatch moves the session back), and
again by `restore`. Workspace-only fingerprints are computed exactly as
before scaffold support, so existing quarantine manifests keep restoring.

--include-recent (preview and apply only; never needed or accepted by list or
restore) must be given to both commands or to neither. A preview run with it
marks every candidate line with a third field,
"CANDIDATE<TAB><id><TAB><fingerprint><TAB>include-recent", and `apply`
refuses the whole request (before creating a batch or moving anything) if
any line's marker disagrees with its own --include-recent flag. This is
needed because the fingerprint deliberately excludes the session
directory's own times, so a fingerprint match alone could not stop a
default preview from being applied with no minimum age. The fingerprint
itself and the manifest entry format are unchanged; the manifest's "scope"
also records "include_recent". Recent empty sessions may have been created
moments ago by a starting Copilot process (or by something else) and may
start being used right after the open-file check; that race is accepted
because quarantine is recoverable, and the preview says so.

Moves use the platform's atomic no-replace rename (macOS renameatx_np with
RENAME_EXCL, Linux renameat2 with RENAME_NOREPLACE) relative to directory
descriptors opened without following symlinks. If that primitive is not
available, nothing is moved.

The session-list lock is an flock(2) on <sessions-file>.lock, shared with
copilot-s; the kernel releases it if the holder dies, so it is never
"stolen" by age.

Limitations (by design): this is not a zero-race guarantee against a native
Copilot process starting to use a session between the final re-validation and
the rename; Copilot's own session index/history (e.g. session-store.db) is
never read or modified, so native history views may still list quarantined
sessions until they are restored.
"""

import argparse
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
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
BATCH_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]{1,9})?Z$"
)
LINE_RE = re.compile(r"^([a-z_]+):(?: (.*))?$")
# Characters that could make a plain YAML scalar mean something other than
# its literal text, or that would corrupt the pipe-delimited session index.
UNSAFE_VALUE_CHARS = set("\"'[]{}|>&*!%@`#\\")

WORKSPACE_FILE = "workspace.yaml"
# The only other layout accepted: the untouched empty scaffold some Copilot
# CLI versions create next to workspace.yaml for a never-used session. It is
# accepted only as a whole (all five top-level entries, nothing else) and
# only with exactly this content; any partial or different layout is kept.
WORKSPACE_LOCK_FILE = ".workspace-fork.lock"
SCAFFOLD_EMPTY_DIRS = ("files", "research")
CHECKPOINTS_DIR = "checkpoints"
CHECKPOINT_INDEX_FILE = "index.md"
EMPTY_CHECKPOINT_INDEX = (
    b"# Checkpoint History\n\n"
    b"Checkpoints are listed in chronological order. Checkpoint 1 is the oldest, "
    b"higher numbers are more recent.\n\n"
    b"| # | Title | File |\n"
    b"|---|-------|------|\n"
)
SCAFFOLD_ENTRIES = sorted(
    [WORKSPACE_FILE, WORKSPACE_LOCK_FILE, CHECKPOINTS_DIR] + list(SCAFFOLD_EMPTY_DIRS)
)
# Paths (relative to the session directory) also passed to the open-file check.
SCAFFOLD_ACTIVITY_PATHS = (
    [WORKSPACE_LOCK_FILE] + list(SCAFFOLD_EMPTY_DIRS)
    + [CHECKPOINTS_DIR, os.path.join(CHECKPOINTS_DIR, CHECKPOINT_INDEX_FILE)]
)
MANIFEST_FILE = "manifest.json"
SESSIONS_SUBDIR = "sessions"
MANIFEST_VERSION = 1
MIN_AGE_SECONDS = 24 * 3600
# Third field of a CANDIDATE/apply line when previewed with --include-recent.
INCLUDE_RECENT_MARKER = "include-recent"
MAX_UPDATE_SKEW_SECONDS = 1.0
MAX_WORKSPACE_BYTES = 64 * 1024
LSOF_TIMEOUT_SECONDS = 15
LOCK_TIMEOUT_SECONDS = 10.0

ALLOWED_KEYS = {
    "id", "cwd", "git_root", "repository", "host_type", "branch",
    "client_name", "last_client_name", "user_named", "summary_count",
    "fork_count", "created_at", "updated_at",
}
REQUIRED_KEYS = ("id", "cwd", "user_named", "created_at", "updated_at")
ZERO_KEYS = ("summary_count", "fork_count")
NAME_KEYS = ("name", "summary")

REASON_ARTIFACTS = "has session history or other files besides workspace.yaml and the empty scaffold"
REASON_CHANGING = "changed while being inspected"
REASON_NAMED = "has a native name or summary"
REASON_METADATA = "metadata not in the expected empty-session form"
REASON_RECENT = "created, updated, or modified within the last 24h"
REASON_FUTURE = "timestamp in the future (clock or metadata problem)"
REASON_USED = "updated after creation (the session was used)"
REASON_ALIAS = "has a copilot-s session-list name"
REASON_ENV = "referenced by the current environment (possibly the active session)"
REASON_OPEN = "open by a running process"
REASON_UNSAFE = "unsafe directory or file (symlink, owner, links, or type)"
REASON_UNREADABLE = "unreadable"
REASON_UNKNOWN_CWD = "directory unknown (no readable workspace.yaml)"

DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Atomic no-replace rename flags.
RENAME_EXCL_DARWIN = 0x00000004
RENAME_NOREPLACE_LINUX = 1
SYS_RENAMEAT2 = {"x86_64": 316, "amd64": 316, "aarch64": 276, "arm64": 276}


class CleanupError(Exception):
    """A fatal, user-visible error: nothing further is attempted."""


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_epoch():
    return time.time()


def display_path(path):
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def parse_timestamp(value):
    match = TIMESTAMP_RE.match(value or "")
    if not match:
        return None
    try:
        base = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    base = base.replace(tzinfo=datetime.timezone.utc)
    fraction = float("0" + match.group(2)) if match.group(2) else 0.0
    return base.timestamp() + fraction


def close_fds(*fds):
    for fd in fds:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def fsync_fd(fd):
    try:
        os.fsync(fd)
    except OSError:
        pass


def open_private_dir(name, dir_fd=None):
    """Open a directory without following a symlink in its final component
    and require it to be a directory owned by this user. Returns an fd."""
    fd = os.open(name, DIR_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise CleanupError("%s is not a directory owned by you" % name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def lexists_at(dir_fd, name):
    try:
        os.lstat(name, dir_fd=dir_fd)
        return True
    except FileNotFoundError:
        return False


def write_file_atomic_at(dir_fd, name, data, mode=0o600):
    """Write bytes to dir_fd/name via an exclusive temp file in the same
    directory + fsync + atomic replace + directory fsync."""
    tmp = ".%s.%s.tmp" % (name, secrets.token_hex(6))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
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


def write_file_atomic(path, data, mode=0o600):
    dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        write_file_atomic_at(dir_fd, os.path.basename(path), data, mode)
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# Atomic no-replace rename
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
# Session index (~/.copilot-sessions) — flock shared with copilot-s
# --------------------------------------------------------------------------

class RegistryLock(object):
    """Exclusive flock(2) on <sessions-file>.lock (a persistent regular file,
    never deleted). Same lock copilot-s takes around its index rewrites; the
    kernel releases it when the holder exits, so no age-based stealing.
    Raises CleanupError if it is not acquired within the timeout."""

    def __init__(self, sessions_file):
        self.path = sessions_file + ".lock"
        self.fd = None

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise CleanupError("cannot open session-list lock %s: %s" % (self.path, exc))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise CleanupError("session-list lock %s is not a regular file owned by you" % self.path)
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
                except OSError as exc:
                    raise CleanupError("cannot lock %s: %s" % (self.path, exc))
                if time.monotonic() >= deadline:
                    raise CleanupError("session list is busy (lock %s held by another process)" % self.path)
                time.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc_info):
        if self.fd is not None:
            os.close(self.fd)  # closing the only descriptor releases the flock
            self.fd = None
        return False


def read_registry_lines(sessions_file):
    """Return the raw index lines (str, surrogateescape-decoded, no newline).

    Missing file -> []. A symlink or non-regular file raises CleanupError
    (fail closed: the index is never rewritten through a symlink).
    """
    try:
        fd = os.open(sessions_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CleanupError("cannot read %s: %s" % (sessions_file, exc))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CleanupError("%s is not a regular file" % sessions_file)
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise CleanupError("cannot read %s: %s" % (sessions_file, exc))
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "surrogateescape").split("\n")


def row_id(line):
    return line.split("|", 1)[0]


def row_name(line):
    parts = line.split("|", 3)
    return parts[1] if len(parts) > 1 else ""


def write_registry_lines(sessions_file, lines):
    try:
        mode = stat.S_IMODE(os.lstat(sessions_file).st_mode)
    except OSError:
        mode = 0o644
    data = "\n".join(lines).encode("utf-8", "surrogateescape")
    write_file_atomic(sessions_file, data, mode)


def add_missing_registry_rows(sessions_file, rows_by_id):
    """Append recorded rows only for IDs that have no row at all."""
    added = []
    with RegistryLock(sessions_file):
        lines = read_registry_lines(sessions_file)
        present = {row_id(line) for line in lines if line}
        additions = []
        for sid, rows in rows_by_id.items():
            if sid in present or not rows:
                continue
            additions.extend(rows)
            added.append(sid)
        if additions:
            body = list(lines)
            while body and body[-1] == "":
                body.pop()
            write_registry_lines(sessions_file, body + additions + [""])
    return added


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------

def safe_value(value):
    if value != value.strip() or value.startswith(("-", "?", ":")):
        return False
    if ": " in value or value.endswith(":"):
        return False
    for ch in value:
        if ch in UNSAFE_VALUE_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
        if 0x80 <= ord(ch) < 0xA0 or ch in ("\u2028", "\u2029", "\ufeff"):
            return False
    return True


def parse_workspace(text):
    """Strict parse of a flat `key: value` workspace.yaml.

    Returns (fields, reason): fields is a dict when the metadata is in the
    expected form, else None with a reason.
    """
    if "\r" in text or "\t" in text:
        return None, REASON_METADATA
    fields = {}
    for line in text.split("\n"):
        if line == "":
            continue
        match = LINE_RE.match(line)
        if not match:
            return None, REASON_METADATA
        key, value = match.group(1), match.group(2) or ""
        if key in fields:
            return None, REASON_METADATA
        if key in NAME_KEYS:
            return None, REASON_NAMED
        if key not in ALLOWED_KEYS or not safe_value(value):
            return None, REASON_METADATA
        fields[key] = value
    for key in REQUIRED_KEYS:
        if not fields.get(key):
            return None, REASON_METADATA
    if fields["user_named"] != "false":
        return None, REASON_NAMED
    for key in ZERO_KEYS:
        if key in fields and fields[key] != "0":
            return None, REASON_NAMED if key == "summary_count" else REASON_METADATA
    cwd = fields["cwd"]
    if not cwd.startswith("/") or os.path.normpath(cwd) != cwd:
        return None, REASON_METADATA
    return fields, None


def lenient_cwd(text):
    """Same extraction copilot-s uses (`^cwd: *`), only for scope filtering."""
    for line in text.split("\n"):
        if line.startswith("cwd:"):
            return line[4:].lstrip(" ")
    return None


def generated_names(cwd):
    base = os.path.basename(cwd) or "/"
    return {"", "unnamed", base + " session"}


def find_lsof():
    found = shutil.which("lsof")
    if found:
        return found
    for candidate in ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def open_state(paths):
    """Return 'closed', 'open', or 'unknown: <why>'. Only a clean, positive
    "no process has these open" result from lsof counts as 'closed'."""
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return "unknown: unsupported platform %s" % sys.platform
    lsof = find_lsof()
    if not lsof:
        return "unknown: lsof not found"
    try:
        result = subprocess.run(
            [lsof, "-n", "-P", "-w", "-F", "p", "--"] + list(paths),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=LSOF_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "unknown: lsof timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown: lsof failed (%s)" % exc
    if any(line.startswith("p") for line in result.stdout.splitlines()):
        return "open"
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return "closed"
    return "unknown: lsof exit %d" % result.returncode


class _ScaffoldProblem(Exception):
    """Internal: the scaffold is not the verified empty one (args[0] = reason)."""


def _stat_key(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read_limited(fd, limit):
    """Read at most limit + 1 bytes (so an oversized file is detectable)."""
    chunks, remaining = [], limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _scaffold_open(name, dir_fd, is_dir, uid):
    """Open dir_fd/name for inspection only if lstat (no follow) shows the
    expected type — so a symlink, FIFO, device, or socket is never opened —
    then open it with O_NOFOLLOW and require the same inode, the expected
    type, ownership by uid, and (files) a single link. Returns (fd, stat)."""
    expected = stat.S_ISDIR if is_dir else stat.S_ISREG
    lst = os.lstat(name, dir_fd=dir_fd)
    if not expected(lst.st_mode):
        raise _ScaffoldProblem(REASON_UNSAFE)
    flags = DIR_OPEN_FLAGS if is_dir else (os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino) or not expected(st.st_mode):
            raise _ScaffoldProblem(REASON_CHANGING)
        if st.st_uid != uid or (not is_dir and st.st_nlink != 1):
            raise _ScaffoldProblem(REASON_UNSAFE)
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _scaffold_file(name, dir_fd, uid, expected_bytes):
    """Verify a scaffold file has exactly expected_bytes and did not change
    while it was read. Returns (stat, fingerprint part)."""
    fd, st = _scaffold_open(name, dir_fd, False, uid)
    try:
        if st.st_size != len(expected_bytes):
            raise _ScaffoldProblem(REASON_ARTIFACTS)
        data = _read_limited(fd, len(expected_bytes))
        if _stat_key(os.fstat(fd)) != _stat_key(st):
            raise _ScaffoldProblem(REASON_CHANGING)
    finally:
        os.close(fd)
    if data != expected_bytes:
        raise _ScaffoldProblem(REASON_ARTIFACTS)
    return st, ["f", name, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns, hashlib.sha256(data).hexdigest()]


def _scaffold_dir(name, dir_fd, uid, expected_entries, inner=None):
    """Verify a scaffold directory lists exactly expected_entries (inner(fd)
    verifies those) and did not change meanwhile. Returns its fingerprint
    part and the stats of everything checked."""
    fd, st = _scaffold_open(name, dir_fd, True, uid)
    try:
        if sorted(os.listdir(fd)) != expected_entries:
            raise _ScaffoldProblem(REASON_ARTIFACTS)
        children = inner(fd) if inner is not None else []
        if sorted(os.listdir(fd)) != expected_entries or _stat_key(os.fstat(fd)) != _stat_key(st):
            raise _ScaffoldProblem(REASON_CHANGING)
    finally:
        os.close(fd)
    stats = [st] + [child[0] for child in children]
    part = ["d", name, st.st_ino, st.st_mode, st.st_uid, st.st_mtime_ns, st.st_ctime_ns,
            [child[1] for child in children]]
    return stats, part


def scan_scaffold(dfd, dst, uid):
    """Verify that the session directory dfd (whose entries are exactly
    SCAFFOLD_ENTRIES and whose fstat was dst) holds only the untouched empty
    scaffold besides workspace.yaml:
      .workspace-fork.lock   regular, empty, single link
      files/, research/      empty directories
      checkpoints/           a directory holding only index.md, which is a
                             single-link regular file whose bytes are exactly
                             EMPTY_CHECKPOINT_INDEX (no checkpoint rows)
    Everything is owned by uid, inspected through descriptors opened without
    following symlinks, and re-checked for changes after it was read.

    Returns (part, problem, newest): part is the fingerprint material (None
    if there is a problem), problem a REASON_* string or None, newest the
    latest mtime/ctime of any scaffold item. Never raises for the content
    found (OSError while inspecting is reported as REASON_UNREADABLE)."""
    try:
        stats, part = [], []
        lock_st, lock_part = _scaffold_file(WORKSPACE_LOCK_FILE, dfd, uid, b"")
        stats.append(lock_st)
        part.append(lock_part)
        for name in SCAFFOLD_EMPTY_DIRS:
            dir_stats, dir_part = _scaffold_dir(name, dfd, uid, [])
            stats.extend(dir_stats)
            part.append(dir_part)

        def checkpoint_index(cfd):
            return [_scaffold_file(CHECKPOINT_INDEX_FILE, cfd, uid, EMPTY_CHECKPOINT_INDEX)]

        dir_stats, dir_part = _scaffold_dir(CHECKPOINTS_DIR, dfd, uid,
                                            [CHECKPOINT_INDEX_FILE], checkpoint_index)
        stats.extend(dir_stats)
        part.append(dir_part)
        # The session directory itself must not have changed meanwhile.
        if sorted(os.listdir(dfd)) != SCAFFOLD_ENTRIES or _stat_key(os.fstat(dfd)) != _stat_key(dst):
            raise _ScaffoldProblem(REASON_CHANGING)
    except _ScaffoldProblem as problem:
        return None, problem.args[0], None
    except OSError:
        return None, REASON_UNREADABLE, None
    newest = max(max(st.st_mtime, st.st_ctime) for st in stats)
    return part, None, newest


def fingerprint_dir(name, dir_fd):
    """Identity + contents fingerprint of the session directory dir_fd/name,
    opened without following symlinks. Excludes the directory's own times,
    which a rename may change; includes its inode, entry names, and (if
    present) workspace.yaml's inode, size, mtime, ctime, and SHA-256.

    For a directory whose entries are exactly SCAFFOLD_ENTRIES it also
    verifies the empty scaffold (scan_scaffold) and adds every scaffold
    item's type, inode, mode, owner, times, and (files) link count, size and
    SHA-256 to the material. A workspace.yaml-only directory hashes exactly
    the same material as before the scaffold was supported, so fingerprints
    recorded in existing quarantine manifests still match.

    Returns (fingerprint, details); details["scaffold_problem"] is a REASON_*
    string when the scaffold entries are present but are not the verified
    empty scaffold (such a directory is never eligible or restorable).
    Raises OSError/CleanupError."""
    dfd = os.open(name, DIR_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        dst = os.fstat(dfd)
        lst = os.lstat(name, dir_fd=dir_fd)
        if (dst.st_dev, dst.st_ino) != (lst.st_dev, lst.st_ino):
            raise CleanupError("directory changed while being inspected")
        entries = sorted(os.listdir(dfd))
        details = {"dir_stat": dst, "entries": entries, "ws_stat": None, "ws_bytes": None,
                   "scaffold": False, "scaffold_problem": None, "scaffold_newest": None}
        ws_part = None
        if WORKSPACE_FILE in entries:
            wfd = os.open(WORKSPACE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            try:
                wst = os.fstat(wfd)
                if not stat.S_ISREG(wst.st_mode):
                    raise CleanupError("workspace.yaml is not a regular file")
                chunks = []
                remaining = MAX_WORKSPACE_BYTES + 1
                while remaining > 0:
                    chunk = os.read(wfd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(wfd)
            details["ws_stat"] = wst
            details["ws_bytes"] = data
            ws_part = [wst.st_ino, wst.st_size, wst.st_mtime_ns, wst.st_ctime_ns,
                       hashlib.sha256(data).hexdigest()]
        material = [dst.st_ino, entries, ws_part]
        if entries == SCAFFOLD_ENTRIES:
            part, problem, newest = scan_scaffold(dfd, dst, os.getuid())
            details["scaffold"] = True
            details["scaffold_problem"] = problem
            details["scaffold_newest"] = newest
            # A fourth, tagged element keeps scaffold material disjoint from
            # the (unchanged) three-element workspace-only material.
            material.append(["empty-scaffold-v1", part] if problem is None
                            else ["invalid-scaffold", problem])
        material = json.dumps(material, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), details
    finally:
        os.close(dfd)


class Context(object):
    def __init__(self, args):
        self.state_dir = os.path.abspath(args.session_state_dir)
        self.sessions_file = os.path.abspath(args.sessions_file)
        self.quarantine_root = os.path.abspath(args.quarantine_dir)
        self.uid = os.getuid()
        # Explicit opt-in (preview/apply only): no minimum age.
        self.include_recent = bool(getattr(args, "include_recent", False))
        self.env_blob = "\n".join(os.environ.values()).lower()
        self.state_fd = None
        self.registry_lines = []
        self.registry_error = None
        self.load_registry()

    def load_registry(self):
        """(Re-)read ~/.copilot-sessions. Called again under the lock right
        before each move so aliases added meanwhile are honored."""
        try:
            self.registry_lines = read_registry_lines(self.sessions_file)
            self.registry_error = None
        except CleanupError as exc:
            self.registry_lines = []
            self.registry_error = str(exc)

    def registry_rows(self, sid):
        return [line for line in self.registry_lines if line and row_id(line) == sid]

    def open_state_dir(self):
        """Open session-state (final component not a symlink, owned by you).
        Returns False if it does not exist."""
        try:
            self.state_fd = open_private_dir(self.state_dir)
        except FileNotFoundError:
            return False
        except CleanupError:
            raise CleanupError("%s is not a real directory owned by you (symlinks are refused)" % self.state_dir)
        except OSError as exc:
            raise CleanupError("cannot open %s: %s (symlinks are refused)" % (self.state_dir, exc))
        return True

    def close(self):
        close_fds(self.state_fd)
        self.state_fd = None


def inspect_session(ctx, sid, scope_cwd, check_activity=True):
    """Evaluate one session directory (ctx.state_fd must be open).

    Returns None if it is out of the requested cwd scope, else a dict with
    keys id, cwd, created_at, fingerprint, reason (reason None = eligible).
    """
    result = {"id": sid, "cwd": None, "created_at": None, "fingerprint": None, "reason": None}
    if not UUID_RE.match(sid):
        result["reason"] = REASON_UNSAFE
        return result
    try:
        lst = os.lstat(sid, dir_fd=ctx.state_fd)
    except OSError:
        if scope_cwd is not None:
            return None
        result["reason"] = REASON_UNREADABLE
        return result
    if not stat.S_ISDIR(lst.st_mode):
        if scope_cwd is not None:
            return None
        result["reason"] = REASON_UNSAFE
        return result
    try:
        fingerprint, details = fingerprint_dir(sid, ctx.state_fd)
    except (OSError, CleanupError):
        result["reason"] = REASON_UNKNOWN_CWD if scope_cwd is not None else REASON_UNREADABLE
        return result

    entries = details["entries"]
    text = strict_text = None
    if details["ws_bytes"] is not None:
        try:
            strict_text = details["ws_bytes"].decode("utf-8")
        except UnicodeDecodeError:
            strict_text = None
        text = strict_text
        if text is None and entries != [WORKSPACE_FILE]:
            # Lenient decoding is only for scope filtering; the metadata
            # checks below use strict_text.
            text = details["ws_bytes"].decode("utf-8", "replace")
    cwd = lenient_cwd(text) if text is not None else None
    result["cwd"] = cwd
    if scope_cwd is not None:
        if cwd is None:
            result["reason"] = REASON_UNKNOWN_CWD
            return result
        if cwd != scope_cwd:
            return None

    # Only workspace.yaml alone, or workspace.yaml plus the complete,
    # verified empty scaffold (details["scaffold"]); anything else is kept.
    if entries != [WORKSPACE_FILE] and not details["scaffold"]:
        result["reason"] = REASON_ARTIFACTS
        return result
    dst, wst = details["dir_stat"], details["ws_stat"]
    if (dst.st_uid != ctx.uid or wst.st_uid != ctx.uid or wst.st_nlink != 1
            or not stat.S_ISREG(wst.st_mode)):
        result["reason"] = REASON_UNSAFE
        return result
    if details["scaffold"] and details["scaffold_problem"] is not None:
        result["reason"] = details["scaffold_problem"]
        return result
    if wst.st_size > MAX_WORKSPACE_BYTES or strict_text is None:
        result["reason"] = REASON_METADATA
        return result
    fields, reason = parse_workspace(strict_text)
    if fields is None:
        result["reason"] = reason
        return result
    if fields["id"] != sid or fields["cwd"] != cwd or "|" in cwd:
        result["reason"] = REASON_METADATA
        return result
    created = parse_timestamp(fields["created_at"])
    updated = parse_timestamp(fields["updated_at"])
    if created is None or updated is None:
        result["reason"] = REASON_METADATA
        return result
    result["created_at"] = fields["created_at"]
    if abs(updated - created) > MAX_UPDATE_SKEW_SECONDS:
        result["reason"] = REASON_USED if updated > created else REASON_METADATA
        return result
    now = now_epoch()
    newest = max(created, updated, dst.st_mtime, dst.st_ctime, wst.st_mtime, wst.st_ctime)
    if details["scaffold"]:
        newest = max(newest, details["scaffold_newest"])
    if ctx.include_recent:
        # Opt-in: only the minimum age is waived. The same timestamps must
        # still not lie in the future; all other checks below still apply.
        if newest > now:
            result["reason"] = REASON_FUTURE
            return result
    elif newest > now - MIN_AGE_SECONDS:
        result["reason"] = REASON_RECENT
        return result

    if ctx.registry_error is not None:
        result["reason"] = "session list unreadable (%s)" % ctx.registry_error
        return result
    allowed = generated_names(cwd)
    if any(row_name(row) not in allowed for row in ctx.registry_rows(sid)):
        result["reason"] = REASON_ALIAS
        return result
    if sid in ctx.env_blob:
        result["reason"] = REASON_ENV
        return result

    if check_activity:
        path = os.path.join(ctx.state_dir, sid)
        paths = [path, os.path.join(path, WORKSPACE_FILE)]
        if details["scaffold"]:
            # Every scaffold item too: e.g. a running Copilot holding
            # .workspace-fork.lock open keeps the session.
            paths.extend(os.path.join(path, rel) for rel in SCAFFOLD_ACTIVITY_PATHS)
        state = open_state(paths)
        if state == "open":
            result["reason"] = REASON_OPEN
            return result
        if state != "closed":
            result["reason"] = "activity check " + state
            return result

    result["fingerprint"] = fingerprint
    return result


# --------------------------------------------------------------------------
# Quarantine directories and manifests
# --------------------------------------------------------------------------

def nearest_existing(path):
    while not os.path.lexists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def check_same_filesystem(ctx):
    try:
        quarantine_dev = os.stat(nearest_existing(ctx.quarantine_root)).st_dev
        state_dev = os.fstat(ctx.state_fd).st_dev
    except OSError as exc:
        raise CleanupError("cannot check quarantine filesystem: %s" % exc)
    if quarantine_dev != state_dev:
        raise CleanupError(
            "quarantine directory %s is not on the same filesystem as %s; "
            "cleanup only moves directories with a same-filesystem rename and never copies"
            % (ctx.quarantine_root, ctx.state_dir)
        )


def open_quarantine_root(ctx, create):
    """Open (optionally creating) the quarantine root: a private real
    directory owned by you, final component not a symlink. Returns an fd,
    or None if it does not exist and create is False."""
    root = ctx.quarantine_root
    if create:
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CleanupError("cannot create %s: %s" % (root, exc))
    try:
        fd = open_private_dir(root)
    except FileNotFoundError:
        if create:
            raise CleanupError("cannot open %s" % root)
        return None
    except (OSError, CleanupError):
        raise CleanupError("%s must be a real directory owned by you (symlinks are refused)" % root)
    if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
        os.close(fd)
        raise CleanupError("%s must be private (run: chmod 700 %s)" % (root, root))
    return fd


def new_batch_name():
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s-%s" % (stamp, secrets.token_hex(4))


def save_manifest(batch, manifest):
    """Durably save the manifest. `batch` is an open batch directory fd (or,
    for manual/recovery use, a batch directory path)."""
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if isinstance(batch, int):
        write_file_atomic_at(batch, MANIFEST_FILE, data)
        return
    batch_fd = open_private_dir(batch)
    try:
        write_file_atomic_at(batch_fd, MANIFEST_FILE, data)
    finally:
        os.close(batch_fd)


class Batch(object):
    """An opened quarantine batch: every path component from the quarantine
    root down is opened without following symlinks and must be owned by you."""

    def __init__(self, root_fd, batch_fd, sessions_fd, manifest):
        self.root_fd, self.batch_fd, self.sessions_fd = root_fd, batch_fd, sessions_fd
        self.manifest = manifest

    def close(self):
        close_fds(self.sessions_fd, self.batch_fd, self.root_fd)


def open_batch(ctx, batch):
    if not BATCH_RE.match(batch or ""):
        raise CleanupError("invalid batch name %r (expected e.g. 20261113-184600-1a2b3c4d)" % batch)
    root_fd = open_quarantine_root(ctx, create=False)
    if root_fd is None:
        raise CleanupError("no quarantine directory at %s" % ctx.quarantine_root)
    batch_fd = sessions_fd = None
    try:
        try:
            batch_fd = open_private_dir(batch, dir_fd=root_fd)
        except (OSError, CleanupError):
            raise CleanupError("batch %s not found (or not a real directory owned by you)" % batch)
        try:
            fd = os.open(MANIFEST_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=batch_fd)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("not a regular file")
                manifest = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise CleanupError("cannot read manifest of batch %s: %s" % (batch, exc))
        if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION \
                or manifest.get("batch") != batch or not isinstance(manifest.get("entries"), list) \
                or not all(isinstance(entry, dict) for entry in manifest["entries"]):
            raise CleanupError("manifest of batch %s is not a valid cleanup manifest" % batch)
        if manifest.get("session_state_dir") != ctx.state_dir:
            raise CleanupError(
                "batch %s was quarantined from %s, not %s; refusing to restore elsewhere"
                % (batch, manifest.get("session_state_dir"), ctx.state_dir)
            )
        try:
            sessions_fd = open_private_dir(SESSIONS_SUBDIR, dir_fd=batch_fd)
        except FileNotFoundError:
            sessions_fd = None
        except (OSError, CleanupError):
            raise CleanupError("batch %s: %s/ is not a real directory owned by you (symlinks are refused)"
                               % (batch, SESSIONS_SUBDIR))
        return Batch(root_fd, batch_fd, sessions_fd, manifest)
    except BaseException:
        close_fds(sessions_fd, batch_fd, root_fd)
        raise


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def scope_of(args):
    if args.all:
        return None
    if not args.cwd or not args.cwd.startswith("/"):
        raise CleanupError("--cwd must be an absolute path (or use --all)")
    return args.cwd


def cmd_preview(args):
    ctx = Context(args)
    try:
        scope_cwd = scope_of(args)
        scope_label = "all directories" if scope_cwd is None else display_path(scope_cwd)
        if ctx.include_recent:
            scope_label += "; including recent sessions"
        print("Empty-session cleanup preview (%s)" % scope_label)
        if not ctx.open_state_dir():
            print("  No session-state directory at %s — nothing to clean up." % display_path(ctx.state_dir))
            return 0
        check_same_filesystem(ctx)

        candidates, skipped = [], Counter()
        for sid in sorted(os.listdir(ctx.state_fd)):
            if not UUID_RE.match(sid):
                continue
            info = inspect_session(ctx, sid, scope_cwd)
            if info is None:
                continue
            if info["reason"]:
                skipped[info["reason"]] += 1
            else:
                candidates.append(info)
        candidates.sort(key=lambda item: (item["created_at"] or "", item["id"]))
    finally:
        ctx.close()

    print("")
    if candidates:
        print("  %d empty session(s) can be moved to quarantine:" % len(candidates))
        for info in candidates:
            where = "  %s" % display_path(info["cwd"]) if scope_cwd is None else ""
            print("    - %s  created %s%s" % (info["id"], info["created_at"], where))
        print("")
        print("  Why each qualifies: only workspace.yaml, alone or with Copilot's untouched")
        print("  empty scaffold (no events/history, checkpoints, or other files),")
        print("  never named (user_named: false, no name/summary, no session-list name),")
        if ctx.include_recent:
            print("  not open by any process. Recent sessions included: no minimum age")
            print("  (timestamps must not be in the future).")
            print("")
            print("  Warning: a recent empty session may have been created moments ago — by a")
            print("  Copilot process that is still starting, or by something else; its origin")
            print("  is not known — and could start being used right after the open-file")
            print("  check. That race is accepted: sessions are only moved to quarantine and")
            print("  can be restored.")
        else:
            print("  created and last changed more than 24h ago, not open by any process.")
    else:
        print("  No empty sessions are eligible for quarantine.")
    if skipped:
        print("")
        print("  Kept (not eligible): %d session(s)" % sum(skipped.values()))
        for reason, count in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])):
            print("    %4d  %s" % (count, reason))
    if candidates:
        print("")
        print("  Destination: %s/<new batch>/sessions/<id>/" % display_path(ctx.quarantine_root))
        print("  Directories are moved (not copied or deleted) and can be restored with:")
        print("    copilot-session-cleanup.py restore --batch <batch>")
        print("  Copilot's own session history/index is not modified and may still list them.")
        marker = "\t" + INCLUDE_RECENT_MARKER if ctx.include_recent else ""
        for info in candidates:
            print("CANDIDATE\t%s\t%s%s" % (info["id"], info["fingerprint"], marker))
    print("")
    return 0


def read_requested(stream, include_recent=False):
    """Parse "<id><TAB><fingerprint>" lines, or — exactly when apply runs
    with --include-recent — "<id><TAB><fingerprint><TAB>include-recent"
    lines. Any policy mismatch rejects the whole request (nothing moved)."""
    requested, seen = [], set()
    for raw in stream.read().split("\n"):
        if raw == "":
            continue
        parts = raw.split("\t")
        line_recent = len(parts) == 3 and parts[2] == INCLUDE_RECENT_MARKER
        if line_recent:
            parts = parts[:2]
        if len(parts) != 2 or not UUID_RE.match(parts[0]) or not FINGERPRINT_RE.match(parts[1]):
            raise CleanupError("malformed candidate line on stdin: %r" % raw)
        if line_recent != include_recent:
            raise CleanupError(
                "candidate %s was previewed %s --include-recent but apply was run %s it; "
                "preview and apply must use the same option (nothing was moved)"
                % (parts[0], "with" if line_recent else "without",
                   "with" if include_recent else "without"))
        if parts[0] in seen:
            raise CleanupError("duplicate candidate %s on stdin" % parts[0])
        seen.add(parts[0])
        requested.append((parts[0], parts[1]))
    return requested


def quarantine_one(ctx, scope_cwd, entry, batch_fd, sessions_fd, manifest):
    """Re-validate and move one previewed session. Holds the session-list
    lock from the final re-validation through the row removal so no alias
    can be added in between. Manifest transitions (each saved durably):
      requested -> skipped
      requested -> pending (+ current list rows recorded) -> moved
                   [registry: removing -> removed | absent | update_failed]
                -> failed | skipped (moved back)
    Returns a warning string if the session list could not be updated.
    Raises OSError if the manifest cannot be saved (caller stops)."""
    sid = entry["id"]
    info = inspect_session(ctx, sid, scope_cwd)
    if info is None or info["reason"] or info["fingerprint"] != entry["fingerprint"]:
        entry["state"] = "skipped"
        entry["reason"] = ("no longer in the selected directory" if info is None
                           else info["reason"] or "changed since the preview")
        save_manifest(batch_fd, manifest)
        return None
    try:
        lock = RegistryLock(ctx.sessions_file)
        lock.__enter__()
    except CleanupError as exc:
        entry["state"], entry["reason"] = "skipped", str(exc)
        save_manifest(batch_fd, manifest)
        return None
    try:
        ctx.load_registry()
        info = inspect_session(ctx, sid, scope_cwd, check_activity=False)
        if info is None or info["reason"] or info["fingerprint"] != entry["fingerprint"]:
            entry["state"] = "skipped"
            entry["reason"] = ("no longer in the selected directory" if info is None
                               else info["reason"] or "changed since the preview")
            save_manifest(batch_fd, manifest)
            return None
        # Durably record the exact rows that will be removed BEFORE moving.
        entry["registry_rows"] = ctx.registry_rows(sid)
        entry["state"] = "pending"
        save_manifest(batch_fd, manifest)
        try:
            rename_noreplace_at(ctx.state_fd, sid, sessions_fd, sid)
        except OSError as exc:
            entry["state"], entry["reason"] = "failed", "move failed: %s" % move_error_text(exc)
            save_manifest(batch_fd, manifest)
            return None
        fsync_fd(ctx.state_fd)
        fsync_fd(sessions_fd)
        try:
            moved_fp, moved_details = fingerprint_dir(sid, sessions_fd)
            if moved_details["scaffold_problem"] is not None:
                moved_fp = None
        except (OSError, CleanupError):
            moved_fp = None
        if moved_fp != entry["fingerprint"]:
            try:
                rename_noreplace_at(sessions_fd, sid, ctx.state_fd, sid)
                entry["state"], entry["reason"] = "skipped", "changed while moving; moved back"
            except OSError as exc:
                entry["state"] = "failed"
                entry["reason"] = ("changed while moving and could not be moved back (%s); "
                                   "left in quarantine for manual review" % move_error_text(exc))
            save_manifest(batch_fd, manifest)
            return None
        entry["state"] = "moved"
        entry["registry"] = "removing" if entry["registry_rows"] else "absent"
        save_manifest(batch_fd, manifest)
        if not entry["registry_rows"]:
            return None
        warning = None
        try:
            kept = [line for line in ctx.registry_lines if not (line and row_id(line) == sid)]
            write_registry_lines(ctx.sessions_file, kept)
            ctx.registry_lines = kept
            entry["registry"] = "removed"
        except (OSError, CleanupError) as exc:
            entry["registry"] = "update_failed"
            warning = "session list not updated for %s (%s)" % (sid, exc)
        save_manifest(batch_fd, manifest)
        return warning
    finally:
        lock.__exit__(None, None, None)


def cmd_apply(args):
    ctx = Context(args)
    try:
        return apply_with_context(ctx, args)
    finally:
        ctx.close()


def apply_with_context(ctx, args):
    scope_cwd = scope_of(args)
    requested = read_requested(sys.stdin, ctx.include_recent)
    if not requested:
        print("Nothing to quarantine.")
        return 0
    if not ctx.open_state_dir():
        raise CleanupError("session-state directory %s does not exist" % ctx.state_dir)
    _no_replace_rename_func()  # fail before creating anything if unsupported
    check_same_filesystem(ctx)

    root_fd = open_quarantine_root(ctx, create=True)
    batch_fd = sessions_fd = None
    try:
        if os.fstat(root_fd).st_dev != os.fstat(ctx.state_fd).st_dev:
            raise CleanupError("quarantine directory is not on the same filesystem as the session state")
        batch = new_batch_name()
        batch_dir = os.path.join(ctx.quarantine_root, batch)
        try:
            os.mkdir(batch, 0o700, dir_fd=root_fd)
            batch_fd = open_private_dir(batch, dir_fd=root_fd)
            os.mkdir(SESSIONS_SUBDIR, 0o700, dir_fd=batch_fd)
            sessions_fd = open_private_dir(SESSIONS_SUBDIR, dir_fd=batch_fd)
        except OSError as exc:
            raise CleanupError("cannot create quarantine batch %s: %s" % (batch_dir, exc))
        entries = [{"id": sid, "fingerprint": fp, "state": "requested", "reason": None,
                    "registry": None, "registry_rows": []} for sid, fp in requested]
        manifest = {
            "version": MANIFEST_VERSION,
            "batch": batch,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_state_dir": ctx.state_dir,
            "sessions_file": ctx.sessions_file,
            "scope": {"all": scope_cwd is None, "cwd": scope_cwd,
                      "include_recent": ctx.include_recent},
            "entries": entries,
        }
        try:
            save_manifest(batch_fd, manifest)
        except OSError as exc:
            raise CleanupError("cannot write manifest in %s: %s (nothing was moved)" % (batch_dir, exc))

        warnings, aborted = [], None
        for entry in entries:
            try:
                warning = quarantine_one(ctx, scope_cwd, entry, batch_fd, sessions_fd, manifest)
            except OSError as exc:
                aborted = "manifest update failed (%s); stopped before further moves" % exc
                break
            if warning:
                warnings.append(warning)
    finally:
        close_fds(sessions_fd, batch_fd, root_fd)

    counts = Counter(entry["state"] for entry in entries)
    print("")
    print("Quarantine batch: %s" % batch)
    print("  Location: %s" % display_path(batch_dir))
    if ctx.include_recent:
        print("  Recent sessions included (no minimum age), as previewed.")
    print("  Moved: %d   Skipped: %d   Failed: %d" % (counts["moved"], counts["skipped"], counts["failed"]))
    for entry in entries:
        if entry["state"] in ("skipped", "failed"):
            print("    %s %s: %s" % (entry["state"], entry["id"], entry["reason"]))
        elif entry["state"] in ("requested", "pending"):
            print("    not completed %s (state %s)" % (entry["id"], entry["state"]))
    for warning in warnings:
        print("  Warning: %s; the moved session may still be listed" % warning)
    if aborted:
        print("  Error: %s" % aborted)
        print("  The manifest records pending moves and list rows; `restore` reconciles them.")
    if counts["moved"] or aborted:
        print("  Restore with: copilot-session-cleanup.py restore --batch %s" % batch)
    return 1 if (counts["failed"] or aborted or warnings) else 0


def cmd_list(args):
    ctx = Context(args)
    root_fd = open_quarantine_root(ctx, create=False)
    if root_fd is None:
        print("No quarantine batches (%s does not exist)." % display_path(ctx.quarantine_root))
        return 0
    try:
        batches = sorted(name for name in os.listdir(root_fd) if BATCH_RE.match(name))
    finally:
        os.close(root_fd)
    if not batches:
        print("No quarantine batches in %s." % display_path(ctx.quarantine_root))
        return 0
    for batch in batches:
        try:
            opened = open_batch(ctx, batch)
        except CleanupError as exc:
            print("%s  (unreadable: %s)" % (batch, exc))
            continue
        opened.close()
        states = Counter(str(entry.get("state")) for entry in opened.manifest["entries"])
        summary = ", ".join("%s %d" % (state, count) for state, count in sorted(states.items()))
        print("%s  %s" % (batch, summary))
    return 0


def confirm(prompt):
    """Exactly "yes" (one line) confirms; anything else, EOF, or an error
    cancels."""
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        answer = sys.stdin.readline()
    except (EOFError, OSError, KeyboardInterrupt):
        return False
    return answer in ("yes\n", "yes")


def cmd_restore(args):
    ctx = Context(args)
    opened = None
    try:
        opened = open_batch(ctx, args.batch)
        if not ctx.open_state_dir():
            raise CleanupError("session-state directory %s does not exist" % ctx.state_dir)
        return restore_batch(ctx, args.batch, opened)
    finally:
        if opened is not None:
            opened.close()
        ctx.close()


def restore_batch(ctx, batch, opened):
    manifest = opened.manifest
    batch_fd, sessions_fd, state_fd = opened.batch_fd, opened.sessions_fd, ctx.state_fd
    _no_replace_rename_func()

    plan, refused, changed = [], [], False
    for entry in manifest["entries"]:
        sid = entry.get("id")
        state = entry.get("state")
        if not isinstance(sid, str) or not UUID_RE.match(sid):
            refused.append((repr(sid), "invalid session ID in manifest"))
            continue
        if state not in ("pending", "moved", "restoring", "failed"):
            continue
        in_quarantine = sessions_fd is not None and lexists_at(sessions_fd, sid)
        in_state = lexists_at(state_fd, sid)
        if not in_quarantine:
            if state == "pending" and in_state:
                entry["state"], entry["reason"] = "skipped", "move never happened (reconciled)"
                changed = True
            elif state == "restoring" and in_state:
                entry["state"] = "restored"
                changed = True
            elif state != "failed":
                refused.append((sid, "missing from quarantine"))
            continue
        if in_state:
            refused.append((sid, "a session directory with this ID already exists"))
            continue
        try:
            close_fds(open_private_dir(sid, dir_fd=sessions_fd))
            current, details = fingerprint_dir(sid, sessions_fd)
        except (OSError, CleanupError) as exc:
            refused.append((sid, "quarantined path is not a safe directory (%s)" % exc))
            continue
        if details["scaffold_problem"] is not None or current != entry.get("fingerprint"):
            refused.append((sid, "quarantined contents changed; restore it manually if intended"))
            continue
        plan.append(entry)

    if changed:
        save_manifest(batch_fd, manifest)
    print("Restore batch %s into %s" % (batch, display_path(ctx.state_dir)))
    for sid, why in refused:
        print("  refused %s: %s" % (sid, why))
    if not plan:
        print("  Nothing to restore.")
        return 1 if refused else 0
    for entry in plan:
        print("  will restore %s" % entry["id"])
    if not confirm("Type 'yes' to restore %d session(s) (anything else cancels): " % len(plan)):
        print("\nCancelled. Nothing was changed.")
        return 0

    restored, failures = [], []
    for entry in plan:
        sid = entry["id"]
        try:
            current, details = fingerprint_dir(sid, sessions_fd)
            if details["scaffold_problem"] is not None or current != entry.get("fingerprint"):
                failures.append((sid, "quarantined contents changed"))
                continue
            previous_state = entry["state"]
            entry["state"] = "restoring"
            save_manifest(batch_fd, manifest)
            try:
                # Atomic no-replace: never clobbers anything (even an empty
                # directory) that appeared at the original path meanwhile.
                rename_noreplace_at(sessions_fd, sid, state_fd, sid)
            except OSError as exc:
                entry["state"] = previous_state
                save_manifest(batch_fd, manifest)
                failures.append((sid, "not restored: %s" % move_error_text(exc)))
                continue
            fsync_fd(sessions_fd)
            fsync_fd(state_fd)
            entry["state"] = "restored"
            save_manifest(batch_fd, manifest)
            restored.append(entry)
        except (OSError, CleanupError) as exc:
            failures.append((sid, str(exc)))
            break

    registry_note = None
    rows = {}
    for entry in restored:
        recorded = entry.get("registry_rows")
        if isinstance(recorded, list):
            rows[entry["id"]] = [row for row in recorded if isinstance(row, str)
                                 and row_id(row) == entry["id"] and "\n" not in row]
    if any(rows.values()):
        try:
            add_missing_registry_rows(ctx.sessions_file, rows)
        except (CleanupError, OSError) as exc:
            registry_note = ("session list rows not re-added (%s); copilot-s re-imports restored "
                             "sessions on next listing, but custom names are in the manifest" % exc)

    print("Restored: %d   Failed: %d" % (len(restored), len(failures)))
    for sid, why in failures:
        print("  failed %s: %s" % (sid, why))
    if registry_note:
        print("  Warning: %s" % registry_note)
    return 1 if (failures or refused or registry_note) else 0


def build_parser():
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(
        prog="copilot-session-cleanup.py",
        description="Recoverable quarantine of empty Copilot CLI sessions (used by copilot-s).",
    )
    parser.add_argument("--session-state-dir", default=os.path.join(home, ".copilot", "session-state"))
    parser.add_argument("--sessions-file", default=os.path.join(home, ".copilot-sessions"))
    parser.add_argument("--quarantine-dir", default=os.path.join(home, ".copilot-s-quarantine"))
    sub = parser.add_subparsers(dest="command")
    for name, help_text in (("preview", "list eligible empty sessions (read-only)"),
                            ("apply", "quarantine the previewed candidates read from stdin")):
        cmd = sub.add_parser(name, help=help_text)
        scope = cmd.add_mutually_exclusive_group(required=True)
        scope.add_argument("--cwd", help="exact session cwd to clean up")
        scope.add_argument("--all", action="store_true", help="all directories")
        cmd.add_argument("--include-recent", action="store_true",
                         help="explicit opt-in: no 24h minimum age (timestamps must still not be "
                              "in the future); preview and apply must both use it or both omit it")
    sub.add_parser("list", help="list quarantine batches")
    restore = sub.add_parser("restore", help="restore a quarantine batch (asks for confirmation)")
    restore.add_argument("--batch", required=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {"preview": cmd_preview, "apply": cmd_apply, "list": cmd_list, "restore": cmd_restore}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command](args)
    except CleanupError as exc:
        print("copilot-session-cleanup: error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
