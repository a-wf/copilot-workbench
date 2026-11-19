#!/usr/bin/env python3
"""copilot_quarantine_purge.py — shared engine for the PERMANENT purge of ONE
quarantine batch (frees disk space; irreversible). Stdlib only.

This file is a library, not a command: copilot-session-cleanup.py (session
quarantine ~/.copilot-s-quarantine, copilot-s menu `x`) and
copilot-report-cleanup.py (run-report quarantine
~/.copilot/copilot-s-report-quarantine, copilot-s menu `r` -> 4) load it from
their own real directory (repo checkout for symlink installs, the install
prefix for --copy installs) and expose it as three subcommands:

  purge-list               Read-only. Every batch with its purge status
                           ("eligible candidate", "not eligible: <why>" or an
                           earlier purge's journal state) plus one machine
                           line "PURGEABLE<TAB><batch>" per candidate. Only a
                           manifest-level check; the preview verifies all.
  purge-preview --batch B  Read-only (no file, folder or lock is created).
                           Full verification and inventory of ONE batch,
                           its estimated reclaimable space, the snapshot id,
                           and machine lines "PURGE_SNAPSHOT<TAB><kind><TAB>
                           <batch><TAB><64-hex digest>" (or
                           "PURGE_INELIGIBLE<TAB><batch>", exit 1).
  purge --batch B [--snapshot DIGEST]
                           Interactive only: needs a real terminal
                           (/dev/tty) and standard output on a terminal
                           (so the preview is visible). Rebuilds and prints the same preview,
                           then asks you to type, on the terminal, exactly
                               PURGE <kind> <batch> <first 12 hex of snapshot>
                           within CONFIRM_TIMEOUT_SECONDS (typed-ahead input
                           is discarded first). There is no yes/--force
                           bypass and nothing is read from stdin. With
                           --snapshot (from purge-preview) it refuses unless
                           the batch is still exactly that snapshot. No lock
                           is held while waiting for you. After the phrase,
                           it takes the quarantine's existing lock, rebuilds
                           the full plan and requires the SAME snapshot
                           digest before the first deletion.

Exit status: 0 purged completely or cancelled (nothing deleted); 1 refused,
error, or partial purge; 130 interrupted.

Eligibility of a batch (fail closed on anything unexpected):
  - exact batch name (YYYYMMDD-HHMMSS-8hex) directly in the quarantine root;
    no purge journal exists for it (an earlier purge is never retried);
  - its manifest passes the helper's strict purge validation and is FULLY
    SETTLED: every entry "moved" or "skipped", at least one "moved"; each
    moved payload is present and still matches the fingerprint recorded when
    it was quarantined (the helper's existing restore/quarantine semantics);
    each skipped entry has nothing in the batch; nothing else is in the
    batch (exact top two levels; deeper levels are covered by the
    fingerprints);
  - every entry, read through descriptors opened relative to their parent
    without following symlinks: a plain directory or a single-link regular
    file, owned by you, not group/world-writable, without setuid/setgid/
    sticky bits or immutable/append-only/no-unlink file flags, directories
    fully accessible to you (permissions are never changed), no directory
    identity seen twice, at most MAX_ENTRIES entries and MAX_DEPTH levels;
  - no mount crossing: same st_dev everywhere AND the same mount as the
    batch folder: macOS compares fstatfs() fsid + mount-on path of every
    directory (layout self-checked: fsid must equal st_dev); Linux compares
    each directory's mnt_id (/proc/self/fdinfo) and refuses any mount point
    at or below the batch (/proc/self/mountinfo), which also catches
    same-device bind mounts. Anything unreadable or another platform fails
    closed;
  - a system lsof (fixed locations, never PATH) positively reports that no
    process has the batch folder or any entry below it open (also catches a
    shell whose working directory is inside). Unavailable/failed/ambiguous
    checks refuse the purge.

Snapshot: SHA-256 over the batch, the quarantine root identity, every
entry's relative name, type, device, inode, mode, owner, link count, size,
allocated blocks, mtime_ns, ctime_ns and file flags, and the helper's
verified manifest/fingerprint summary. The inventory is taken twice around
the manifest/fingerprint verification and the open-file check and must be
identical (any write changes a ctime). The size estimate is the sum of
st_blocks x 512 of every entry — approximate (APFS clones/snapshots, open
files, compression, metadata) — and deletion is not secure erasure.

Locks (never held while waiting for you; all bounded waits):
  sessions  the existing session-list lock (flock on <sessions-file>.lock,
            shared with copilot-s and cleanup `apply`/`restore`). `restore`
            now performs its moves under this lock and re-checks the purge
            journal there, so a purge and a restore of the same batch
            exclude each other; `apply` records each moved entry's final
            manifest state under it, so a settled manifest cannot change.
  reports   the existing quarantine .transaction.lock, which `apply` and
            `restore` hold for their whole run. The reconcile/run/capacity
            locks guard the canonical report and publication paths, which a
            purge never touches (no live folder or lock is created), and
            producers never write into the quarantine, so they are not
            needed; a stale holder of a quarantined run's .lock is caught by
            the open-file check.

Journal (outside the batch, under the private control folder
<quarantine root>/.purge/, 0700): <batch>.json, created with a no-replace
link before the first deletion and durably updated: started -> complete, or
started -> partial (error, first failing entry, counts). Any journal (any
state, or unreadable) makes `restore`, `purge` and `purge-preview` of that
batch refuse, so a crash at any point can never lead to a misleading restore
or an automatic retry/resume. `list` shows it additively; machine lines are
unchanged. Journals are compact (no file names besides the failing entry)
and are kept.

Deletion: only inventoried entries, bottom-up, each with unlinkat/rmdir
relative to its parent's descriptor after re-checking (no follow) that the
entry still has the snapshot's identity and metadata and that every parent
from the batch folder down is still bound where it was. Payload first,
then the payload folders, then the manifest, then the batch folder; the
quarantine root and the control folder are never removed. No recursive
utility, no newly listed target is ever deleted (an unexpected entry makes
rmdir fail), permissions are never changed. The first discrepancy, I/O
error, deadline or Ctrl-C stops the purge, records "partial" and reports how
much is left (a removal in flight at a Ctrl-C counts as possibly done). A
Ctrl-C while that result is journaled and printed is deferred until both
are done; once the journal may exist, no outcome is ever reported as
"nothing was deleted" and nothing suggests a retry.

Limitations (by design): a hostile process running as you that swaps an
entry between the final check and unlink(2) cannot be excluded portably by
POSIX; the cooperating copilot-s tools are excluded by the locks above and
everything else must be quiescent (positively checked with lsof). Linux
`chattr +i` attributes and ACLs denying deletion are not inspected; such an
entry stops the purge part-way (reported as partial).
"""

import contextlib
import ctypes
import ctypes.util
import datetime
import hashlib
import json
import os
import platform
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time

try:
    import termios
except ImportError:  # pragma: no cover - POSIX only
    termios = None

BATCH_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

CONTROL_DIR = ".purge"          # mirrored as PURGE_CONTROL_DIR in both helpers
JOURNAL_SUFFIX = ".json"
JOURNAL_KIND = "copilot-s.quarantine-purge"
JOURNAL_VERSION = 1
JOURNAL_STATES = ("started", "partial", "complete")
JOURNAL_MAX_BYTES = 64 * 1024
SNAPSHOT_SHORT = 12

MAX_ENTRIES = 20000
MAX_DEPTH = 24
MAX_LISTED_BATCHES = 1000
MAX_LISTED_JOURNALS = 5000
MAX_ITEM_LINES = 100
MOUNTINFO_MAX_BYTES = 4 * 1024 * 1024
PLAN_BUDGET_SECONDS = 180
DELETE_BUDGET_SECONDS = 300
CONFIRM_TIMEOUT_SECONDS = 120
CONFIRM_MAX_BYTES = 1024
LSOF_TIMEOUT_SECONDS = 15
LSOF_MAX_PATHS = 400
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof", "/bin/lsof")
REASON_MAX_CHARS = 300

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW | CLOEXEC
FILE_READ_FLAGS = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | CLOEXEC
SPECIAL_MODE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
BLOCKING_FLAGS = 0
for _flag in ("UF_IMMUTABLE", "UF_APPEND", "UF_NOUNLINK", "SF_IMMUTABLE", "SF_APPEND", "SF_NOUNLINK"):
    BLOCKING_FLAGS |= getattr(stat, _flag, 0)


class PurgeError(Exception):
    """Fatal: nothing (further) is deleted."""


class Refused(Exception):
    """The batch is not eligible right now; nothing was deleted."""


class _Stop(Exception):
    """Internal: a discrepancy during deletion (args = reason, rel)."""


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def display_path(path):
    home = os.path.expanduser("~")
    if home and home != "/" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def human_bytes(count):
    value = float(count)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return ("%d %s" % (count, unit)) if unit == "bytes" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%d bytes" % count


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_json(obj):
    data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def clip(text):
    text = str(text)
    return text if len(text) <= REASON_MAX_CHARS else text[:REASON_MAX_CHARS - 1] + "…"


def rel_text(rel):
    """Printable relative path (control characters escaped)."""
    if not rel:
        return "(batch folder)"
    out = []
    for part in rel:
        safe = "".join(ch if (ch.isprintable() and ch not in "\\") else "\\x%02x" % (ord(ch) & 0xff)
                       for ch in part)
        out.append(safe)
    return clip("/".join(out))


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


def _ident(st):
    return (st.st_dev, st.st_ino)


def _key(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size,
            getattr(st, "st_blocks", -1), st.st_mtime_ns, st.st_ctime_ns, getattr(st, "st_flags", 0))


def _flags(st):
    return getattr(st, "st_flags", 0) & BLOCKING_FLAGS


# --------------------------------------------------------------------------
# Mount identification (macOS: fstatfs; Linux: mnt_id + mountinfo)
# --------------------------------------------------------------------------

class _Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _DarwinStatfs(ctypes.Structure):
    """struct statfs with 64-bit inodes (sys/mount.h, MNAMELEN 1024)."""
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", _Fsid), ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


_DARWIN_FSTATFS = None


def _darwin_fstatfs_func():
    global _DARWIN_FSTATFS
    if _DARWIN_FSTATFS is not None:
        return _DARWIN_FSTATFS
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError as exc:
        raise PurgeError("cannot load the C library to identify mounts (%s); nothing was deleted" % exc)
    name = "fstatfs$INODE64" if platform.machine() == "x86_64" else "fstatfs"
    try:
        func = libc[name]
    except AttributeError:
        raise PurgeError("cannot identify mounts on this macOS (%s unavailable); nothing was deleted" % name)
    func.argtypes = [ctypes.c_int, ctypes.c_void_p]
    func.restype = ctypes.c_int
    _DARWIN_FSTATFS = func
    return func


def _darwin_mount_of(fd):
    func = _darwin_fstatfs_func()
    # Oversized buffer: a layout mismatch can never write past it, and the
    # fsid == st_dev self-check below rejects a misread layout.
    buf = ctypes.create_string_buffer(8192)
    if func(fd, ctypes.addressof(buf)) != 0:
        err = ctypes.get_errno()
        raise PurgeError("cannot identify the mount (fstatfs: %s); nothing was deleted" % os.strerror(err))
    sfs = _DarwinStatfs.from_buffer(buf)
    dev = os.fstat(fd).st_dev
    on = bytes(sfs.f_mntonname)
    if (sfs.f_fsid.val[0] & 0xffffffff) != (dev & 0xffffffff) or not on.startswith(b"/"):
        raise PurgeError("cannot identify the mount reliably (unexpected fstatfs result); nothing was deleted")
    return ("darwin", sfs.f_fsid.val[0], sfs.f_fsid.val[1], bytes(sfs.f_fstypename), on)


def _linux_mount_of(fd):
    try:
        with open("/proc/self/fdinfo/%d" % fd, "rb") as handle:
            data = handle.read(64 * 1024)
    except OSError as exc:
        raise PurgeError("cannot identify the mount (/proc/self/fdinfo: %s); nothing was deleted"
                         % (exc.strerror or exc))
    for line in data.split(b"\n"):
        if line.startswith(b"mnt_id:"):
            value = line[len(b"mnt_id:"):].strip()
            if value.isdigit():
                return ("linux", int(value))
    raise PurgeError("cannot identify the mount (no mnt_id in /proc/self/fdinfo; Linux 3.15+ required); "
                     "nothing was deleted")


def _unescape_mount(raw):
    return re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8) & 0xff]), raw)


class MountGuard(object):
    """Every directory of the batch must be on the batch folder's mount."""

    def __init__(self, batch_fd, batch_path):
        if sys.platform == "darwin":
            self._mount_of = _darwin_mount_of
        elif sys.platform.startswith("linux"):
            self._mount_of = _linux_mount_of
        else:
            raise PurgeError("permanent purge supports only macOS and Linux (this is %s); nothing was deleted"
                             % sys.platform)
        self.base = self._mount_of(batch_fd)
        real = os.path.realpath(batch_path)
        try:
            rst = os.stat(real)
        except OSError:
            raise Refused("the batch path does not resolve to the opened batch folder")
        if _ident(rst) != _ident(os.fstat(batch_fd)):
            raise Refused("the batch path does not resolve to the opened batch folder")
        if sys.platform.startswith("linux"):
            self._check_mountinfo(os.fsencode(real))

    @staticmethod
    def _check_mountinfo(real):
        try:
            with open("/proc/self/mountinfo", "rb") as handle:
                data = handle.read(MOUNTINFO_MAX_BYTES + 1)
        except OSError as exc:
            raise PurgeError("cannot read /proc/self/mountinfo to rule out mounts inside the batch (%s); "
                             "nothing was deleted" % (exc.strerror or exc))
        if len(data) > MOUNTINFO_MAX_BYTES:
            raise PurgeError("/proc/self/mountinfo is too large to check; nothing was deleted")
        prefix = real.rstrip(b"/") + b"/"
        for line in data.split(b"\n"):
            if not line:
                continue
            fields = line.split(b" ")
            if len(fields) < 5:
                raise PurgeError("unexpected /proc/self/mountinfo format; nothing was deleted")
            point = _unescape_mount(fields[4])
            if point == real or point.startswith(prefix):
                raise Refused("a filesystem is mounted at or inside this batch (%s)"
                              % clip(point.decode("utf-8", "replace")))

    def check_dir(self, fd, rel):
        if self._mount_of(fd) != self.base:
            raise Refused("%s is on another mount (mount crossing)" % rel_text(rel))


# --------------------------------------------------------------------------
# Inventory (descriptor-relative, no-follow, bounded)
# --------------------------------------------------------------------------

class Node(object):
    __slots__ = ("rel", "kind", "st", "children")

    def __init__(self, rel, kind, st, children=None):
        self.rel, self.kind, self.st, self.children = rel, kind, st, children

    def key(self):
        return _key(self.st)

    def ident(self):
        return _ident(self.st)

    def material(self):
        return ["/".join(os.fsencode(part).hex() for part in self.rel), self.kind] + list(self.key())


class Inventory(object):
    def __init__(self):
        self.nodes, self.order = {}, []

    def add(self, node):
        self.nodes[node.rel] = node
        self.order.append(node)

    @property
    def root(self):
        return self.nodes[()]

    def materials(self):
        return [node.material() for node in self.order]

    def totals(self, rels=None):
        """(files, folders, apparent bytes, allocated bytes) of the whole
        inventory or of the subtrees rooted at `rels`."""
        files = dirs = apparent = allocated = 0
        for node in self.order:
            if rels is not None and not any(node.rel[:len(r)] == r for r in rels):
                continue
            if node.kind == "f":
                files += 1
                apparent += node.st.st_size
            else:
                dirs += 1
            allocated += node.st.st_blocks * 512
        return files, dirs, apparent, allocated


def _check_entry(st, rel, uid, dev):
    mode = st.st_mode
    where = rel_text(rel)
    if stat.S_ISLNK(mode):
        raise Refused("%s is a symlink" % where)
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise Refused("%s is not a plain file or folder (FIFO, socket or device)" % where)
    if st.st_uid != uid:
        raise Refused("%s is not owned by you" % where)
    if st.st_dev != dev:
        raise Refused("%s is on another filesystem" % where)
    if mode & 0o022:
        raise Refused("%s is group/world-writable" % where)
    if mode & SPECIAL_MODE_BITS:
        raise Refused("%s has a setuid/setgid/sticky bit" % where)
    if _flags(st):
        raise Refused("%s has an immutable/append-only/no-unlink flag" % where)
    if stat.S_ISREG(mode) and st.st_nlink != 1:
        raise Refused("%s has other hard links" % where)
    if stat.S_ISDIR(mode) and (mode & 0o700) != 0o700:
        raise Refused("%s is not fully accessible to you (purge never changes permissions)" % where)
    if getattr(st, "st_blocks", None) is None:
        raise PurgeError("this platform reports no allocated blocks; nothing was deleted")


class _Walker(object):
    def __init__(self, deadline):
        self.deadline = deadline
        self.uid = os.getuid()
        self.inventory = Inventory()
        self.seen_dirs = set()
        self.count = 0
        self.mounts = None

    def _tick(self):
        if time.monotonic() > self.deadline:
            raise Refused("not fully inspected (time budget reached; try again)")

    def _list(self, fd, rel):
        """First visit of a folder: its sorted entry names. Refuses as soon as
        the entries already counted (self.count, which does not yet include
        these) plus these exceed MAX_ENTRIES; since every listed entry is
        counted exactly once afterwards, this refuses iff the batch really
        holds more than MAX_ENTRIES entries (the batch folder itself is not
        counted)."""
        limit = MAX_ENTRIES - self.count
        names = []
        with os.scandir(fd) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) > limit:
                    raise Refused("more than %d entries; too many to purge safely" % MAX_ENTRIES)
        names.sort()
        return names

    @staticmethod
    def _lists_exactly(fd, names):
        """Verification relist (not counted again): True only when fd lists
        exactly `names`. Reads at most len(names) + 1 entries, so extra
        entries added meanwhile can neither grow memory nor time unbounded."""
        seen = []
        with os.scandir(fd) as it:
            for entry in it:
                seen.append(entry.name)
                if len(seen) > len(names):
                    return False
        seen.sort()
        return seen == names

    def walk(self, root_fd, batch, batch_path):
        try:
            lst = os.stat(batch, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise Refused("batch %s not found" % batch)
        if not stat.S_ISDIR(lst.st_mode):
            raise Refused("batch %s is not a plain folder (symlinks are refused)" % batch)
        try:
            fd = os.open(batch, DIR_FLAGS, dir_fd=root_fd)
        except OSError:
            raise Refused("batch %s is not a plain folder (symlinks are refused)" % batch)
        try:
            st = os.fstat(fd)
            if _key(st) != _key(lst):
                raise Refused("the batch folder changed while being inspected")
            _check_entry(st, (), self.uid, os.fstat(root_fd).st_dev)
            if stat.S_IMODE(st.st_mode) & 0o077:
                raise Refused("the batch folder is not private (0700)")
            self.mounts = MountGuard(fd, batch_path)
            self._dir(fd, (), st, 0)
        except OSError as exc:
            raise Refused("unreadable while being inspected (%s)" % (exc.strerror or exc))
        finally:
            os.close(fd)
        return self.inventory

    def _dir(self, fd, rel, st, depth):
        if _ident(st) in self.seen_dirs:
            raise Refused("%s appears twice (repeated folder identity)" % rel_text(rel))
        self.seen_dirs.add(_ident(st))
        names = self._list(fd, rel)
        self.inventory.add(Node(rel, "d", st, names))
        for name in names:
            self._tick()
            crel = rel + (name,)
            try:
                cst = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                raise Refused("%s changed while being inspected" % rel_text(crel))
            _check_entry(cst, crel, self.uid, st.st_dev)
            self.count += 1
            if self.count > MAX_ENTRIES:
                raise Refused("more than %d entries; too many to purge safely" % MAX_ENTRIES)
            if stat.S_ISDIR(cst.st_mode):
                if depth + 1 > MAX_DEPTH:
                    raise Refused("folders nested deeper than %d levels" % MAX_DEPTH)
                try:
                    cfd = os.open(name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    raise Refused("%s changed while being inspected" % rel_text(crel))
                try:
                    if _key(os.fstat(cfd)) != _key(cst):
                        raise Refused("%s changed while being inspected" % rel_text(crel))
                    self.mounts.check_dir(cfd, crel)
                    self._dir(cfd, crel, cst, depth + 1)
                finally:
                    os.close(cfd)
            else:
                self.inventory.add(Node(crel, "f", cst))
        if _key(os.fstat(fd)) != _key(st) or not self._lists_exactly(fd, names):
            raise Refused("%s changed while being inspected" % rel_text(rel))


def take_inventory(root_fd, batch, batch_path, deadline):
    return _Walker(deadline).walk(root_fd, batch, batch_path)


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


def open_state(paths, deadline):
    """'closed', 'open' or 'unknown: <why>' for the given absolute paths
    (every file AND folder is passed explicitly; lsof does not recurse).
    Only exit 1 with empty stdout and stderr counts as 'closed'."""
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return "unknown: unsupported platform %s" % sys.platform
    lsof = find_lsof()
    if lsof is None:
        return "unknown: no system lsof found"
    paths = list(paths)
    for start in range(0, len(paths), LSOF_MAX_PATHS):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            return "unknown: time budget reached"
        try:
            result = run_lsof([lsof, "-n", "-P", "-w", "-F", "p", "--"] + paths[start:start + LSOF_MAX_PATHS],
                              min(LSOF_TIMEOUT_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            return "unknown: lsof timed out"
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return "unknown: lsof failed (%s)" % type(exc).__name__
        out = result.stdout.decode("ascii", "replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
        err = result.stderr.decode("ascii", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
        if any(line.startswith("p") for line in out.splitlines()):
            return "open"
        if result.returncode == 1 and not out.strip() and not err.strip():
            continue
        return "unknown: lsof exit %d" % result.returncode
    return "closed"


# --------------------------------------------------------------------------
# Journal (<quarantine root>/.purge/<batch>.json)
# --------------------------------------------------------------------------

def _open_control(root_fd, create):
    """Descriptor of the private control folder, or None when it does not
    exist (and `create` is False). Raises PurgeError when unsafe."""
    if create:
        try:
            os.mkdir(CONTROL_DIR, 0o700, dir_fd=root_fd)
            fsync_fd(root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PurgeError("cannot create the purge control folder %s (%s); nothing was deleted"
                             % (CONTROL_DIR, exc.strerror or exc))
    try:
        fd = os.open(CONTROL_DIR, DIR_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        if create:
            raise PurgeError("cannot open the purge control folder; nothing was deleted")
        return None
    except OSError:
        raise PurgeError("the purge control folder %s is not a plain folder (symlinks are refused)" % CONTROL_DIR)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) & 0o077 or (st.st_mode & 0o700) != 0o700):
            raise PurgeError("the purge control folder %s must be a private (0700) folder owned by you; "
                             "refused, not repaired" % CONTROL_DIR)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_journal_at(cfd, name):
    """Parsed journal dict, or {"state": "unreadable"}."""
    try:
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=cfd)
    except OSError:
        return {"state": "unreadable"}
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                or st.st_size > JOURNAL_MAX_BYTES):
            return {"state": "unreadable"}
        data = os.read(fd, JOURNAL_MAX_BYTES + 1)
    except OSError:
        return {"state": "unreadable"}
    finally:
        os.close(fd)
    try:
        journal = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {"state": "unreadable"}
    if (not isinstance(journal, dict) or journal.get("kind") != JOURNAL_KIND
            or type(journal.get("version")) is not int or journal.get("version") != JOURNAL_VERSION
            or journal.get("batch") != name[:-len(JOURNAL_SUFFIX)]
            or journal.get("state") not in JOURNAL_STATES):
        return {"state": "unreadable"}
    return journal


def journal_state(root_fd, batch):
    """None when no purge of `batch` was ever started, else the journal dict
    (state "unreadable" when it exists but cannot be trusted). An unsafe
    control folder counts as an unreadable journal (fail closed)."""
    try:
        cfd = _open_control(root_fd, create=False)
    except PurgeError:
        return {"state": "unreadable"}
    if cfd is None:
        return None
    try:
        name = batch + JOURNAL_SUFFIX
        try:
            os.stat(name, dir_fd=cfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return {"state": "unreadable"}
        return _read_journal_at(cfd, name)
    finally:
        os.close(cfd)


def journal_refusal(journal, batch_path):
    state = journal.get("state")
    where = display_path(batch_path)
    if state == "complete":
        return "this batch was already purged (journal: complete); it is never purged twice"
    if state == "partial":
        return ("a permanent purge of this batch stopped part-way (%s); part of its data is deleted. It is "
                "never retried or resumed automatically and cannot be restored by this tool — review what is "
                "left in %s yourself" % (clip(journal.get("error") or "unknown error"), where))
    if state == "started":
        return ("a permanent purge of this batch was started and never finished (interrupted or crashed); part "
                "of its data may be deleted. It is never retried or resumed automatically and cannot be "
                "restored by this tool — review what is left in %s yourself" % where)
    return ("its purge journal (%s/%s%s) is unreadable; refused (fail closed)"
            % (CONTROL_DIR, os.path.basename(batch_path), JOURNAL_SUFFIX))


class Journal(object):
    def __init__(self, root_fd, adapter, plan):
        self.root_fd = root_fd
        self.name = plan.batch + JOURNAL_SUFFIX
        files, dirs, apparent, allocated = plan.inventory.totals()
        self.data = {
            "kind": JOURNAL_KIND, "version": JOURNAL_VERSION, "quarantine": adapter.kind,
            "batch": plan.batch, "snapshot": plan.digest, "state": "started",
            "started_at": utc_now_iso(), "updated_at": utc_now_iso(),
            "planned": {"files": files, "folders": dirs, "apparent_bytes": apparent,
                        "allocated_bytes_estimate": allocated},
            "deleted": {"files": 0, "folders": 0}, "stopped_at": None, "error": None,
        }
        self.ino = None

    def _write_tmp(self, cfd):
        tmp = ".%s.%s.tmp" % (self.name, secrets.token_hex(6))
        data = (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC, 0o600, dir_fd=cfd)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp, dir_fd=cfd)
            except OSError:
                pass
            raise
        os.close(fd)
        return tmp

    def create(self):
        """Durably create the journal with a no-replace link (an existing
        journal — any purge ever started for this batch — refuses)."""
        cfd = _open_control(self.root_fd, create=True)
        try:
            try:
                tmp = self._write_tmp(cfd)
            except OSError as exc:
                raise PurgeError("cannot write the purge journal (%s); nothing was deleted" % (exc.strerror or exc))
            try:
                os.link(tmp, self.name, src_dir_fd=cfd, dst_dir_fd=cfd)
            except FileExistsError:
                raise PurgeError("a purge journal for this batch already exists; nothing was deleted")
            except (OSError, NotImplementedError) as exc:
                raise PurgeError("cannot create the purge journal (%s); nothing was deleted"
                                 % (getattr(exc, "strerror", None) or exc))
            finally:
                try:
                    os.unlink(tmp, dir_fd=cfd)
                except OSError:
                    pass
            fsync_fd(cfd)
            st = os.stat(self.name, dir_fd=cfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
                raise PurgeError("the purge journal is not a private single-link file; nothing was deleted")
            self.ino = _ident(st)
        finally:
            os.close(cfd)

    def update(self, state, deleted_files, deleted_dirs, stopped_at=None, error=None):
        """Atomically replace OUR journal (same identity as created) with the
        new state. Raises PurgeError when that is not possible."""
        self.data.update({"state": state, "updated_at": utc_now_iso(),
                          "deleted": {"files": deleted_files, "folders": deleted_dirs},
                          "stopped_at": stopped_at, "error": clip(error) if error else None})
        try:
            cfd = _open_control(self.root_fd, create=False)
            if cfd is None:
                raise PurgeError("the purge control folder disappeared")
        except PurgeError as exc:
            raise PurgeError("cannot update the purge journal (%s)" % exc)
        try:
            st = os.stat(self.name, dir_fd=cfd, follow_symlinks=False)
            if _ident(st) != self.ino or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise PurgeError("the purge journal was replaced by something else; not overwritten")
            tmp = self._write_tmp(cfd)
            try:
                os.replace(tmp, self.name, src_dir_fd=cfd, dst_dir_fd=cfd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=cfd)
                except OSError:
                    pass
                raise
            fsync_fd(cfd)
            self.ino = _ident(os.stat(self.name, dir_fd=cfd, follow_symlinks=False))
        except OSError as exc:
            raise PurgeError("cannot update the purge journal (%s)" % (exc.strerror or exc))
        finally:
            os.close(cfd)


# --------------------------------------------------------------------------
# Plan (read-only)
# --------------------------------------------------------------------------

class Verification(object):
    """What a helper adapter returns from verify():
      identity   (st_dev, st_ino) of the batch folder it validated
      expected   {rel tuple: "d"|"f"} for EVERY entry at depth 1 and 2
      last_names top-level file names deleted after all payload (manifest)
      items      [(list of rel tuples, label)] shown in the preview
      semantic   JSON-able summary of the verified manifest/fingerprints
      moved      number of moved (payload) entries"""

    def __init__(self, identity, expected, last_names, items, semantic, moved):
        self.identity, self.expected, self.last_names = identity, expected, tuple(last_names)
        self.items, self.semantic, self.moved = items, semantic, moved


class Plan(object):
    def __init__(self, batch, batch_path, root_ident, inventory, verification, digest):
        self.batch, self.batch_path, self.root_ident = batch, batch_path, root_ident
        self.inventory, self.verification, self.digest = inventory, verification, digest


def _check_root(adapter, root_fd):
    st = os.fstat(root_fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077
            or (st.st_mode & 0o700) != 0o700 or _flags(st)):
        raise Refused("the quarantine folder must be a private (0700) folder owned by you; refused, not repaired")
    try:
        pst = os.stat(adapter.root_path)
    except OSError:
        raise Refused("the quarantine path no longer resolves to the opened quarantine folder")
    if _ident(pst) != _ident(st):
        raise Refused("the quarantine path no longer resolves to the opened quarantine folder")
    return _ident(st)


def _check_layout(inventory, verification):
    actual = {node.rel: node.kind for node in inventory.order if 1 <= len(node.rel) <= 2}
    expected = verification.expected
    for rel in sorted(actual):
        if rel not in expected:
            raise Refused("unexpected %s in the batch: %s" % ("folder" if actual[rel] == "d" else "file",
                                                              rel_text(rel)))
        if expected[rel] != actual[rel]:
            raise Refused("%s has an unexpected type" % rel_text(rel))
    for rel in sorted(expected):
        if rel not in actual:
            raise Refused("%s is missing from the batch" % rel_text(rel))
    for name in verification.last_names:
        if actual.get((name,)) != "f":
            raise Refused("%s is missing from the batch" % name)


def build_plan(adapter, root_fd, batch, deadline):
    """Full read-only verification + inventory + snapshot digest of ONE
    batch. Raises Refused (not eligible) or PurgeError."""
    if not BATCH_RE.match(batch or ""):
        raise Refused("invalid batch name %r (expected e.g. 20261119-120000-1a2b3c4d)" % batch)
    batch_path = os.path.join(adapter.root_path, batch)
    root_ident = _check_root(adapter, root_fd)
    journal = journal_state(root_fd, batch)
    if journal is not None:
        raise Refused(journal_refusal(journal, batch_path))
    first = take_inventory(root_fd, batch, batch_path, deadline)
    verification = adapter.verify(root_fd, batch, first, deadline)
    if verification.identity != first.root.ident():
        raise Refused("the batch folder changed while being inspected")
    if verification.moved < 1:
        raise Refused("nothing was moved into this batch; there is nothing to purge")
    _check_layout(first, verification)
    paths = [os.path.join(batch_path, *node.rel) if node.rel else batch_path for node in first.order]
    state = open_state(paths, deadline)
    if state == "open":
        raise Refused("a running process has a file or folder of this batch open (or uses it as its "
                      "working directory)")
    if state != "closed":
        raise Refused("open-file check %s — a purge needs a positive \"nothing open\" result" % state)
    second = take_inventory(root_fd, batch, batch_path, deadline)
    if second.materials() != first.materials():
        raise Refused("the batch changed while being inspected")
    digest = sha256_json({"v": 1, "kind": adapter.kind, "batch": batch, "root": list(root_ident),
                          "entries": second.materials(), "verified": verification.semantic})
    return Plan(batch, batch_path, root_ident, second, verification, digest)


def print_plan(adapter, plan):
    inventory, verification = plan.inventory, plan.verification
    files, dirs, apparent, allocated = inventory.totals()
    print("Permanent purge preview — %s, batch %s" % (adapter.title, plan.batch))
    print("  Location: %s" % display_path(plan.batch_path))
    print("  Verified: fully settled manifest (%d moved; every other entry skipped and absent), every"
          % verification.moved)
    print("  payload unchanged since it was quarantined, nothing else in the batch, plain private files")
    print("  on one mount, and no process has any of it open.")
    print("  Contents that will be DELETED:")
    for rels, label in verification.items[:MAX_ITEM_LINES]:
        ifiles, idirs, _iapp, ialloc = inventory.totals(rels)
        print("    - %s  (%d file(s), %d folder(s), ~%s allocated)" % (label, ifiles, idirs, human_bytes(ialloc)))
    if len(verification.items) > MAX_ITEM_LINES:
        print("    … and %d more" % (len(verification.items) - MAX_ITEM_LINES))
    print("    - plus the batch's own manifest and folders (deleted last)")
    print("  Total: %d file(s) and %d folder(s), %s of file data." % (files, dirs, human_bytes(apparent)))
    print("  Estimated reclaimable space: about %s allocated on disk." % human_bytes(allocated))
    print("  This is an ESTIMATE (sum of each entry's st_blocks x 512): the space actually released can")
    print("  differ — APFS clones and snapshots (incl. Time Machine local snapshots), files another process")
    print("  still holds open, compression and filesystem metadata. Deletion is NOT secure erasure.")
    print("  Irreversible: a purged batch cannot be restored by any copilot-s tool.")
    print("  Snapshot: %s" % plan.digest)


# --------------------------------------------------------------------------
# Confirmation on the real terminal
# --------------------------------------------------------------------------

def _open_tty():
    if termios is None:
        raise PurgeError("permanent purge needs an interactive terminal; nothing was deleted")
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0) | CLOEXEC)
    except OSError:
        raise PurgeError("permanent purge needs an interactive terminal (/dev/tty); nothing was deleted")
    if not os.isatty(fd):
        os.close(fd)
        raise PurgeError("permanent purge needs an interactive terminal (/dev/tty); nothing was deleted")
    return fd


def confirm_on_tty(tty_fd, phrase, timeout):
    """True when exactly `phrase` was typed (one line) within `timeout`
    seconds, False for anything else, None on timeout or end of input."""
    try:
        termios.tcflush(tty_fd, termios.TCIFLUSH)  # never accept typed-ahead input
    except (termios.error, OSError):
        pass
    prompt = ("\nTo PERMANENTLY delete this batch, type exactly the following line and press Enter\n"
              "(anything else, Enter alone, or no answer within %d seconds cancels):\n    %s\n> "
              % (timeout, phrase))
    try:
        os.write(tty_fd, prompt.encode("utf-8"))
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf and len(buf) <= CONFIRM_MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = select.select([tty_fd], [], [], remaining)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            chunk = os.read(tty_fd, CONFIRM_MAX_BYTES)
        except OSError:
            return None
        if not chunk:
            return None if not buf else False
        buf += chunk
    line = buf.split(b"\n", 1)[0].rstrip(b"\r")
    return line == phrase.encode("utf-8")


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------

class _Deleter(object):
    def __init__(self, root_fd, plan, deadline):
        self.root_fd, self.plan, self.deadline = root_fd, plan, deadline
        self.nodes = plan.inventory.nodes
        self.files = self.dirs = 0
        self.current = ()
        self.mounts = None
        # rel of the removal whose outcome is unknown (set just before the
        # unlink/rmdir call, cleared once it is counted or has failed): an
        # interruption while it is set may or may not have removed it.
        self.in_flight = None

    def _remove(self, parent_fd, name, rel, is_dir):
        """The only place anything is removed (test seam: os.unlink/os.rmdir).
        A raised OSError means it was not removed."""
        self.in_flight = rel
        try:
            if is_dir:
                os.rmdir(name, dir_fd=parent_fd)  # fails (ENOTEMPTY) if anything new appeared inside
            else:
                os.unlink(name, dir_fd=parent_fd)
        except OSError:
            self.in_flight = None
            raise
        if is_dir:
            self.dirs += 1
        else:
            self.files += 1
        self.in_flight = None

    def _tick(self, rel):
        self.current = rel
        if time.monotonic() > self.deadline:
            raise _Stop("time budget reached", rel)

    def _verify_chain(self, chain, rel):
        """Every folder from the batch down to the current parent is still
        bound at its place with the snapshot identity."""
        for parent_fd, name, fd, ident in chain:
            lst = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _ident(lst) != ident or not stat.S_ISDIR(lst.st_mode) or _ident(os.fstat(fd)) != ident:
                raise _Stop("a parent folder was moved or replaced", rel)

    def run(self):
        root = self.nodes[()]
        batch = self.plan.batch
        self._tick(())
        lst = os.stat(batch, dir_fd=self.root_fd, follow_symlinks=False)
        if _key(lst) != root.key():
            raise _Stop("the batch folder changed since the confirmed snapshot", ())
        bfd = os.open(batch, DIR_FLAGS, dir_fd=self.root_fd)
        try:
            if _ident(os.fstat(bfd)) != root.ident():
                raise _Stop("the batch folder was replaced", ())
            try:
                self.mounts = MountGuard(bfd, self.plan.batch_path)
            except Refused as exc:
                raise _Stop(str(exc), ())
            chain = [(self.root_fd, batch, bfd, root.ident())]
            last = self.plan.verification.last_names
            self._children(bfd, root, chain, skip=last)
            for name in last:
                self._file(bfd, (name,), chain)
            fsync_fd(bfd)
        finally:
            os.close(bfd)
        self._tick(())
        lst = os.stat(batch, dir_fd=self.root_fd, follow_symlinks=False)
        if _ident(lst) != root.ident() or not stat.S_ISDIR(lst.st_mode):
            raise _Stop("the batch folder was moved or replaced", ())
        self._remove(self.root_fd, batch, (), True)
        fsync_fd(self.root_fd)

    def _children(self, fd, node, chain, skip=()):
        for name in node.children:
            if name in skip:
                continue
            child = self.nodes.get(node.rel + (name,))
            if child is None:
                raise _Stop("not in the confirmed inventory", node.rel + (name,))
            if child.kind == "d":
                self._dir(fd, child, chain)
            else:
                self._file(fd, child.rel, chain)
        fsync_fd(fd)

    def _dir(self, parent_fd, node, chain):
        name = node.rel[-1]
        self._tick(node.rel)
        self._verify_chain(chain, node.rel)
        lst = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _key(lst) != node.key():
            raise _Stop("changed since the confirmed snapshot", node.rel)
        cfd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        try:
            if _ident(os.fstat(cfd)) != node.ident():
                raise _Stop("replaced while being opened", node.rel)
            try:
                self.mounts.check_dir(cfd, node.rel)
            except Refused as exc:
                raise _Stop(str(exc), node.rel)
            self._children(cfd, node, chain + [(parent_fd, name, cfd, node.ident())])
        finally:
            os.close(cfd)
        self._tick(node.rel)
        self._verify_chain(chain, node.rel)
        lst = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _ident(lst) != node.ident() or not stat.S_ISDIR(lst.st_mode) or lst.st_uid != node.st.st_uid:
            raise _Stop("moved or replaced before removal", node.rel)
        self._remove(parent_fd, name, node.rel, True)

    def _file(self, parent_fd, rel, chain):
        node = self.nodes.get(rel)
        if node is None or node.kind != "f":
            raise _Stop("not a file of the confirmed inventory", rel)
        self._tick(rel)
        self._verify_chain(chain, rel)
        lst = os.stat(rel[-1], dir_fd=parent_fd, follow_symlinks=False)
        if _key(lst) != node.key():
            raise _Stop("changed since the confirmed snapshot", rel)
        self._remove(parent_fd, rel[-1], rel, False)


def _execute(adapter, root_fd, plan):
    journal = Journal(root_fd, adapter, plan)
    # From here on run() never claims that nothing was deleted: first the
    # journal may exist, then entries may be gone (see _report_after_start).
    _RUN_STATE.update(journal_possible=True, batch_path=plan.batch_path,
                      journal_path=os.path.join(adapter.root_path, CONTROL_DIR, journal.name))
    journal.create()  # PurgeError here: nothing was deleted
    deleter = _Deleter(root_fd, plan, time.monotonic() + DELETE_BUDGET_SECONDS)
    _RUN_STATE["deletion_possible"] = True
    outcome = None
    try:
        deleter.run()
    except (KeyboardInterrupt, Exception) as exc:  # Exception: e.g. PurgeError from a mount re-check
        outcome = exc
    # A Ctrl-C while the journal and the summary are written is deferred
    # until both describe the outcome; it is then raised and run() reports it
    # without retracting that result.
    with _sigint_deferred():
        code = _finish(adapter, plan, journal, deleter, outcome)
        _RUN_STATE["reported"] = True
        return code


def _finish(adapter, plan, journal, deleter, outcome):
    """Record the outcome in the journal and print it. Conservative: a removal
    that was in flight when the purge stopped counts as possibly done."""
    uncertain = deleter.in_flight
    interrupted = isinstance(outcome, KeyboardInterrupt)
    error = stopped_at = None
    if isinstance(outcome, _Stop):
        stopped_at = rel_text(outcome.args[1])
        error = "%s: %s" % (stopped_at, outcome.args[0])
    elif isinstance(outcome, OSError):
        stopped_at = rel_text(deleter.current)
        error = "%s: %s" % (stopped_at, outcome.strerror or outcome)
    elif interrupted:
        stopped_at = rel_text(uncertain if uncertain is not None else deleter.current)
        error = "%s: interrupted (Ctrl-C)" % stopped_at
    elif outcome is not None:
        stopped_at = rel_text(deleter.current)
        error = "%s: %s" % (stopped_at, outcome)
    if error is not None and uncertain is not None:
        error += " (its removal may or may not have happened)"
    files, dirs, _apparent, allocated = plan.inventory.totals()
    print("")
    if error is None:
        note = None
        try:
            journal.update("complete", deleter.files, deleter.dirs)
        except PurgeError as exc:
            note = str(exc)
        print("Purged batch %s permanently: deleted %d file(s) and %d folder(s)." % (plan.batch, deleter.files,
                                                                                 deleter.dirs))
        print("  About %s allocated on disk was released (estimate; see the preview notes)." % human_bytes(allocated))
        print("  A compact record is kept in %s/%s/%s." % (display_path(adapter.root_path), CONTROL_DIR,
                                                          journal.name))
        if note:
            print("  Warning: %s; the journal still says \"started\", so this batch name stays blocked." % note)
        return 0
    try:
        journal.update("partial", deleter.files, deleter.dirs, stopped_at, error)
        recorded = "recorded as \"partial\""
    except PurgeError as exc:
        recorded = "the journal still says \"started\" (%s)" % exc
    left = (files + dirs) - (deleter.files + deleter.dirs)
    maybe = "" if uncertain is None else " (plus possibly %s)" % rel_text(uncertain)
    print("PURGE STOPPED part-way: %s" % error)
    print("  Deleted %d file(s) and %d folder(s)%s; about %d entr%s remain in %s."
          % (deleter.files, deleter.dirs, maybe, left, "y" if left == 1 else "ies", display_path(plan.batch_path)))
    if deleter.files or deleter.dirs or uncertain is not None:
        print("  This batch is now PARTIALLY deleted: it can no longer be restored by this tool, and the")
        print("  purge is never retried or resumed automatically (%s). Review what is left yourself." % recorded)
    else:
        print("  Nothing was deleted, but the purge was started (%s): the batch stays blocked for" % recorded)
        print("  restore and purge; review it yourself.")
    return 130 if interrupted else 1


# State of the current run() for conservative reporting of anything that
# escapes after the purge may have changed something (set by _execute).
_RUN_STATE = {}


@contextlib.contextmanager
def _sigint_deferred():
    """Block SIGINT (Ctrl-C) for the block; a pending one is delivered when
    the previous signal mask is restored. No-op where unsupported."""
    mask = getattr(signal, "pthread_sigmask", None)
    previous = None
    if mask is not None:
        try:
            previous = mask(signal.SIG_BLOCK, {signal.SIGINT})
        except (OSError, ValueError):
            previous = None
    try:
        yield
    finally:
        if previous is not None:
            mask(signal.SIG_SETMASK, previous)


def _report_after_start(adapter, what, code):
    """Result for anything escaping once the journal may exist: never claims
    that nothing was deleted, never suggests a retry."""
    batch_path = display_path(_RUN_STATE.get("batch_path") or "the batch")
    journal_path = display_path(_RUN_STATE.get("journal_path") or CONTROL_DIR)
    lines = ["", "%s: %s" % (adapter.prog, clip(what))]
    if _RUN_STATE.get("reported"):
        lines.append("  This happened after the purge result above was recorded; that result stands.")
    elif _RUN_STATE.get("deletion_possible"):
        lines.append("  The permanent purge had already started deleting: part or all of %s may be gone." % batch_path)
        lines.append("  Its journal %s blocks restore and any new purge of this batch; it is never retried"
                     % journal_path)
        lines.append("  or resumed automatically. Review what is left yourself.")
    else:
        lines.append("  No entry of %s was deleted, but the purge journal %s may already exist; if it does,"
                     % (batch_path, journal_path))
        lines.append("  it blocks restore and any new purge of this batch. Review it yourself.")
    try:
        print("\n".join(lines), file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        pass
    return code


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _list_names(root_fd):
    names, truncated = [], False
    with os.scandir(root_fd) as it:
        for entry in it:
            if BATCH_RE.match(entry.name):
                if len(names) >= MAX_LISTED_BATCHES:
                    truncated = True
                    break
                names.append(entry.name)
    names.sort()
    return names, truncated


def _list_journals(root_fd):
    try:
        cfd = _open_control(root_fd, create=False)
    except PurgeError as exc:
        return None, str(exc)
    if cfd is None:
        return {}, None
    journals = {}
    try:
        with os.scandir(cfd) as it:
            for entry in it:
                name = entry.name
                if name.endswith(JOURNAL_SUFFIX) and BATCH_RE.match(name[:-len(JOURNAL_SUFFIX)]):
                    if len(journals) >= MAX_LISTED_JOURNALS:
                        break
                    journals[name[:-len(JOURNAL_SUFFIX)]] = _read_journal_at(cfd, name)
    finally:
        os.close(cfd)
    return journals, None


def cmd_purge_list(adapter):
    root_fd = adapter.open_root()
    location = display_path(adapter.root_path)
    if root_fd is None:
        print("No quarantine batches (%s does not exist)." % location)
        return 0
    eligible = []
    try:
        names, truncated = _list_names(root_fd)
        journals, journal_error = _list_journals(root_fd)
        print("Permanent purge — %s in %s" % (adapter.title, location))
        if journal_error:
            print("  Purge control folder problem: %s — every purge is refused until you fix it." % journal_error)
        if not names:
            print("  No quarantine batches.")
        for name in names:
            if journals is None:
                print("  %s  purge status unknown (purge control folder problem above)" % name)
                continue
            journal = journal_state(root_fd, name)
            if journal is not None:
                print("  %s  purge %s — NOT restorable; never retried automatically; review it yourself"
                      % (name, journal.get("state")))
                continue
            try:
                ok, text = adapter.manifest_status(root_fd, name)
            except Refused as exc:
                ok, text = False, str(exc)
            print("  %s  %s%s" % (name, "candidate: " if ok else "not eligible: ", text))
            if ok:
                eligible.append(name)
        if truncated:
            print("  Note: only the first %d batches are listed." % MAX_LISTED_BATCHES)
        completed = 0
        for name, journal in sorted((journals or {}).items()):
            if name in names:
                continue
            if journal.get("state") == "complete":
                completed += 1
            else:
                print("  %s  purge %s — batch folder no longer complete/present; review it yourself"
                      % (name, journal.get("state")))
        if completed:
            print("  Purged earlier: %d batch(es) (compact journals in %s/%s)." % (completed, location, CONTROL_DIR))
        print("  Only fully settled batches (every entry moved or skipped, at least one moved) can be purged.")
        print("  The preview re-verifies everything and shows the estimated reclaimable space first.")
    finally:
        os.close(root_fd)
    for name in eligible:
        print("PURGEABLE\t%s" % name)
    return 0


def cmd_purge_preview(adapter, batch):
    root_fd = adapter.open_root()
    if root_fd is None:
        raise Refused("no quarantine at %s" % display_path(adapter.root_path))
    try:
        plan = build_plan(adapter, root_fd, batch, time.monotonic() + PLAN_BUDGET_SECONDS)
    finally:
        os.close(root_fd)
    print_plan(adapter, plan)
    print("  Read-only preview: nothing was changed. To purge, run (interactive terminal required):")
    print("    %s purge --batch %s --snapshot %s" % (adapter.prog, batch, plan.digest))
    print("PURGE_SNAPSHOT\t%s\t%s\t%s" % (adapter.kind, batch, plan.digest))
    return 0


def cmd_purge(adapter, batch, snapshot):
    if not BATCH_RE.match(batch or ""):
        raise PurgeError("invalid batch name %r; nothing was deleted" % batch)
    if snapshot is not None and not DIGEST_RE.match(snapshot):
        raise PurgeError("--snapshot must be the 64-hex snapshot printed by purge-preview; nothing was deleted")
    tty_fd = _open_tty()
    try:
        if not sys.stdout.isatty():
            raise PurgeError("purge shows its preview on standard output, which must be the terminal (do not "
                             "redirect or pipe it); nothing was deleted")
        root_fd = adapter.open_root()
        if root_fd is None:
            raise Refused("no quarantine at %s" % display_path(adapter.root_path))
        try:
            plan = build_plan(adapter, root_fd, batch, time.monotonic() + PLAN_BUDGET_SECONDS)
            print_plan(adapter, plan)
            if snapshot is not None and snapshot != plan.digest:
                raise Refused("the batch is no longer the snapshot you previewed (%s…); review the preview "
                              "above and run the purge again" % snapshot[:SNAPSHOT_SHORT])
            sys.stdout.flush()
            phrase = "PURGE %s %s %s" % (adapter.kind, batch, plan.digest[:SNAPSHOT_SHORT])
            try:
                answer = confirm_on_tty(tty_fd, phrase, CONFIRM_TIMEOUT_SECONDS)
            except KeyboardInterrupt:
                print("\nCancelled. Nothing was deleted.")
                return 130
            if answer is None:
                print("\nCancelled (no answer). Nothing was deleted.")
                return 0
            if not answer:
                print("Cancelled (the phrase did not match exactly). Nothing was deleted.")
                return 0
            with adapter.lock(root_fd):
                fresh = build_plan(adapter, root_fd, batch, time.monotonic() + PLAN_BUDGET_SECONDS)
                if fresh.digest != plan.digest:
                    raise Refused("the batch changed after you confirmed (new snapshot %s…)"
                                  % fresh.digest[:SNAPSHOT_SHORT])
                return _execute(adapter, root_fd, fresh)
        finally:
            os.close(root_fd)
    finally:
        os.close(tty_fd)


def run(adapter, command, batch=None, snapshot=None):
    """Entry point used by the helpers. Returns the exit status."""
    _RUN_STATE.clear()
    try:
        if command == "purge-list":
            return cmd_purge_list(adapter)
        if command == "purge-preview":
            return cmd_purge_preview(adapter, batch)
        if command == "purge":
            return cmd_purge(adapter, batch, snapshot)
        raise PurgeError("unknown purge command %r" % command)
    except KeyboardInterrupt:
        if _RUN_STATE.get("journal_possible"):
            return _report_after_start(adapter, "interrupted (Ctrl-C)", 130)
        print("\nInterrupted. Nothing was deleted by this run.", file=sys.stderr)
        return 130
    except Refused as exc:
        if _RUN_STATE.get("journal_possible"):
            return _report_after_start(adapter, str(exc), 1)
        print("Not purged — %s: %s. Nothing was deleted." % (batch or "batch", exc))
        if command == "purge-preview":
            print("PURGE_INELIGIBLE\t%s" % (batch if BATCH_RE.match(batch or "") else "-"))
        return 1
    except PurgeError as exc:
        if _RUN_STATE.get("journal_possible"):
            return _report_after_start(adapter, "error: %s" % exc, 1)
        print("%s: error: %s" % (adapter.prog, exc), file=sys.stderr)
        return 1
    except OSError as exc:
        if _RUN_STATE.get("journal_possible"):
            return _report_after_start(adapter, "error: %s" % (exc.strerror or exc), 1)
        # Before the purge journal could exist: nothing was deleted yet.
        print("%s: error: %s; nothing was deleted" % (adapter.prog, exc.strerror or exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    print("copilot_quarantine_purge.py is a library used by copilot-session-cleanup.py and "
          "copilot-report-cleanup.py (subcommands purge-list, purge-preview, purge).", file=sys.stderr)
    sys.exit(2)
