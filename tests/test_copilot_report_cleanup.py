"""Recoverable run-report cleanup, isolated HOME / synthetic producers only.

Positive records come from the real reporter's start -> finish protocol with
the suite's fake RTK, not fabricated finalized records. Most activity probes
use the run_lsof seam; one explicitly labelled test uses only system lsof and
owned fixture paths. No installed CLI, network, real HOME, or other repo is
accessed. Run with unittest from tests/ (matching the existing suite).
"""
import contextlib
import copy
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import types
import unittest
import uuid
from unittest import mock

import test_copilot_s as menu_fixture
import test_copilot_s_rtk_export as export_fixture
import test_copilot_session_report as report_fixture


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin/copilot-report-cleanup.py"
RID = "11111111-1111-4111-8111-111111111111"
FP = "a" * 64
BATCH = "20261119-120000-1234abcd"


def load_cleanup():
    spec = importlib.util.spec_from_file_location("report_cleanup_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_snapshot(root):
    """Only caller-owned fixtures: bytes, inode and non-atime metadata."""
    root = Path(root)
    paths = [root, *sorted(root.rglob("*"))] if root.exists() else []
    out = {}
    for path in paths:
        st = path.lstat()
        content = (os.readlink(path) if path.is_symlink() else
                   path.read_bytes() if stat.S_ISREG(st.st_mode) else None)
        out[str(path.relative_to(root))] = (
            st.st_ino, st.st_mode, st.st_uid, st.st_nlink, st.st_size,
            st.st_mtime_ns, content)
    return out


class FixtureCrash(BaseException):
    """Simulated process death, bypassing ordinary rollback handlers."""


class CleanupHome(export_fixture.ExportHome):
    def setUp(self):
        super().setUp()
        self.cleanup = load_cleanup()
        self.quarantine = self.home / ".copilot/copilot-s-report-quarantine"
        # ctime cannot safely be backdated; advance only the cleanup clock.
        self.cleanup_now = time.time() + 2 * 86400
        patch = mock.patch.object(self.cleanup, "now_epoch", return_value=self.cleanup_now)
        self.cleanup_clock = patch.start()
        self.addCleanup(patch.stop)
        self.lsof_calls = []
        self.lsof_patch = mock.patch.object(self.cleanup, "run_lsof", side_effect=self.closed_lsof)
        self.lsof_patch.start()
        self.addCleanup(self.lsof_patch.stop)
        self.find_patch = mock.patch.object(self.cleanup, "find_lsof", return_value="/usr/sbin/lsof")
        self.find_patch.start()
        self.addCleanup(self.find_patch.stop)

    def closed_lsof(self, argv, timeout):
        self.lsof_calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    def command(self, *args, data=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(data)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.cleanup.main(list(map(str, args)))
        return types.SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def create(self, index=0, mode="rtk", code=0):
        rec = self.start_real_run(index, mode=mode, otel=True)
        self.write_real_evidence(rec, conversations=(export_fixture.SID,), hook=(
            export_fixture.SID,) if mode == "rtk" else None)
        if mode == "rtk":
            self.gain(rec["run_id"])
        epoch = self.clock.return_value
        self.invoke("finish", "--run-id", rec["run_id"], "--exit-code", code,
                    "--launched-epoch", epoch, "--ended-epoch", epoch,
                    "--session-state-dir", self.states)
        final = self.record(rec["run_id"])
        self.assertEqual(final["state"], self.cleanup.outcome_from_exit(code)[0])
        report_fixture.write(self.path(final["run_id"]) / "raw/private-tee.txt",
                             "PRIVATE_TEE_SENTINEL\n")
        return final

    def preview(self, recent=False):
        result = self.command("preview", *(["--include-recent"] if recent else []))
        self.assert_ok(result)
        lines = [line for line in result.stdout.splitlines() if line.startswith("CANDIDATE\t")]
        return result, lines

    def select(self, lines):
        # Forward the exact tagged preview lines, including any recent marker.
        return "".join(line + "\n" for line in lines)

    def apply_lines(self, lines, recent=False):
        return self.command("apply", *(["--include-recent"] if recent else []), data=self.select(lines))

    def batch(self):
        paths = list(self.quarantine.glob("*/manifest.json"))
        self.assertEqual(len(paths), 1, paths)
        path = paths[0]
        return path.parent, json.loads(path.read_bytes())

    def moved(self, mode="rtk"):
        rec = self.create(mode=mode)
        _, lines = self.preview()
        self.assertEqual(len(lines), 1)
        self.assert_ok(self.apply_lines(lines))
        batch, manifest = self.batch()
        self.assertEqual(manifest["entries"][0]["state"], "moved")
        return rec, batch, manifest

    def child(self, code, *args):
        proc = subprocess.Popen([sys.executable, "-u", "-c", code, *map(str, args)],
                                cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_child, proc)
        return proc

    @staticmethod
    def stop_child(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for handle in (proc.stdin, proc.stdout, proc.stderr):
            if handle is not None:
                handle.close()

    def holder(self, path, lock=False):
        proc = self.child(
            "import fcntl,sys\n"
            "f=open(sys.argv[1], 'a+')\n"
            "if sys.argv[2]=='lock': fcntl.flock(f,fcntl.LOCK_EX)\n"
            "print('READY',flush=True)\nsys.stdin.read()\n",
            path, "lock" if lock else "open")
        self.assertEqual(proc.stdout.readline().strip(), "READY")
        return proc


class TestPreviewAndSelection(CleanupHome):
    def test_preview_and_list_on_nonexistent_roots_create_nothing(self):
        before = tree_snapshot(self.home)
        result, lines = self.preview()
        self.assertEqual(lines, [])
        self.assertIn("ALL workspaces", result.stdout)
        self.assertIn("private raw/", result.stdout)
        self.assert_ok(self.command("list"))
        self.assertEqual(tree_snapshot(self.home), before)
        self.assertEqual(self.lsof_calls, [])

    def test_real_normal_rtk_age_policy_and_readonly_preview(self):
        normal = self.create(mode="normal")
        rtk = self.create(1)
        self.assertEqual(rtk["rtk"]["preflight"]["smoke_test"], "passed")
        self.assertTrue(self.target(rtk).exists())
        self.assertFalse(self.target(normal).exists())
        before = tree_snapshot(self.home)
        out, lines = self.preview()
        self.assertEqual({line.split("\t")[1] for line in lines},
                         {normal["run_id"], rtk["run_id"]})
        for line in lines:
            self.assertRegex(line, r"^CANDIDATE\t[0-9a-f-]{36}\t[0-9a-f]{64}$")
        self.assertIn("NOT freed", out.stdout)
        self.assertIn("global RTK", out.stdout)
        self.assertIn("2 run report(s)", out.stdout)
        self.assertEqual(tree_snapshot(self.home), before)
        self.assertFalse(self.quarantine.exists())
        # Old finalization alone is insufficient: fresh private entry ctime.
        self.cleanup_clock.return_value = time.time()
        kept, no_lines = self.preview()
        self.assertEqual(no_lines, [])
        self.assertIn("files changed within the last 24h", kept.stdout)
        _, recent = self.preview(recent=True)
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(line.endswith("\tinclude-recent") for line in recent))

    def test_recent_finalization_requires_optin_but_future_never_qualifies(self):
        self.clock.return_value = int(time.time())
        rec = self.create(mode="normal")
        self.cleanup_clock.return_value = time.time() + 2
        result, lines = self.preview()
        self.assertEqual(lines, [])
        self.assertIn("finalized within the last 24h", result.stdout)
        _, lines = self.preview(recent=True)
        self.assertEqual(len(lines), 1)
        self.cleanup_clock.return_value = self.clock.return_value - 1
        out, lines = self.preview(recent=True)
        self.assertEqual(lines, [])
        self.assertIn("future", out.stdout)
        self.assertTrue(self.path(rec["run_id"]).exists())

    def test_recent_rtk_apply_requires_matching_optin_and_quarantines_both_sources(self):
        self.clock.return_value = int(time.time()) - 3600
        rec = self.create(mode="rtk")
        self.cleanup_clock.return_value = time.time() + 2
        age = self.cleanup_clock.return_value - self.cleanup.epoch_of_iso(rec["finalized_at"])
        self.assertGreater(age, 0)
        self.assertLess(age, 24 * 3600)
        run = self.path(rec["run_id"])
        publication = self.target(rec)
        run_before = tree_snapshot(run)
        publication_before = tree_snapshot(publication)
        self.assertTrue(publication.is_file())
        _, lines = self.preview(recent=True)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^CANDIDATE\t%s\t[0-9a-f]{64}\tinclude-recent$" % rec["run_id"])
        before_rejection = tree_snapshot(self.home)
        refused = self.apply_lines(lines)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("must use the same option", refused.stderr)
        self.assertEqual(tree_snapshot(self.home), before_rejection)
        self.assertFalse(self.quarantine.exists())
        applied = self.apply_lines(lines, recent=True)
        self.assert_ok(applied)
        self.assertIn("Moved: 1", applied.stdout)
        batch, manifest = self.batch()
        self.assertTrue(manifest["include_recent"])
        self.assertEqual([(entry["id"], entry["state"]) for entry in manifest["entries"]],
                         [(rec["run_id"], "moved")])
        self.assertFalse(run.exists())
        self.assertFalse(publication.exists())
        self.assertEqual(tree_snapshot(batch / "runs" / rec["run_id"]), run_before)
        self.assertEqual(tree_snapshot(batch / "publications" / publication.name), publication_before)

    def test_strict_invalid_records_fail_closed_from_real_producer_seed(self):
        normal = self.create(mode="normal")
        rtk = self.create(1)
        cases = [
            (normal, ("schema_version",), True, "supported run record"),
            (normal, ("exit_code",), False, "inconsistent"),
            (normal, ("exit_code",), 256, "inconsistent"),
            (normal, ("exit_code",), -1, "inconsistent"),
            (normal, ("schema",), "unknown", "supported run record"),
            (normal, ("run_id",), RID, "supported run record"),
            (normal, ("mode",), "unknown", "supported run record"),
            (normal, ("launch", "kind"), "unknown", "supported run record"),
            (normal, ("state",), "incomplete", "not finalized"),
            (normal, ("state",), "report-failure", "finalization failed"),
            (normal, ("state",), "unknown", "unknown run state"),
            (normal, ("state_reason",), "other", "inconsistent"),
            (normal, ("copilot_outcome",), "interrupted", "inconsistent"),
            (normal, ("created_at",), "2099-01-01T00:00:00Z", "out of order"),
            (normal, ("ended_at",), "2026-11-10T12:00:00+00:00", "timestamps"),
            (normal, ("finalized_at",), None, "timestamps"),
            (rtk, ("rtk", "preflight", "smoke_test"), "failed", "supported run record"),
        ]
        for seed, keys, value, reason in cases:
            with self.subTest(field=keys, value=value):
                record = copy.deepcopy(seed)
                target = record
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                problem, _ = self.cleanup.record_eligibility(
                    json.dumps(record).encode(), seed["run_id"], self.cleanup_now, False)
                self.assertIsNotNone(problem)
                self.assertIn(reason, problem)
        # Whole preview also keeps incomplete and unknown/malformed records.
        for rec, content in ((normal, b"[]"), (rtk, b"{")):
            (self.path(rec["run_id"]) / "run.json").write_bytes(content)
        _, lines = self.preview(recent=True)
        self.assertEqual(lines, [])

    def test_real_error_and_signal_outcomes_remain_eligible(self):
        for index, code in enumerate((0, 7, 126, 127, 129, 130, 131, 143, 255)):
            self.create(index, mode="normal", code=code)
        _, lines = self.preview()
        self.assertEqual(len(lines), 9)

    def test_current_environment_and_unexpected_normal_publication_are_kept(self):
        normal = self.create(mode="normal")
        rtk = self.create(1)
        with mock.patch.dict(os.environ, {"COPILOT_S_RUN_ID": rtk["run_id"]}):
            out, lines = self.preview()
            self.assertEqual([line.split("\t")[1] for line in lines], [normal["run_id"]])
            self.assertIn("current environment", out.stdout)
        pub = json.loads(self.target(rtk).read_bytes())
        pub["run"]["run_id"] = normal["run_id"]
        report_fixture.write(self.target(normal), json.dumps(pub))
        out, lines = self.preview()
        self.assertEqual([line.split("\t")[1] for line in lines], [rtk["run_id"]])
        self.assertIn("publication exists for a normal-mode run", out.stdout)

    def test_publication_future_changes_are_refused_even_with_recent_optin(self):
        rec = self.create()
        future = self.cleanup_now + 300
        os.utime(self.target(rec), (future, future))
        for recent in (False, True):
            with self.subTest(recent=recent):
                out, lines = self.preview(recent)
                self.assertEqual(lines, [], out.stdout)
                self.assertIn("future", out.stdout)

    def test_raw_future_and_oversized_record_are_kept(self):
        rec = self.create(mode="normal")
        raw = self.path(rec["run_id"]) / "raw/private-tee.txt"
        os.utime(raw, (self.cleanup_now + 10, self.cleanup_now + 10))
        out, lines = self.preview(recent=True)
        self.assertEqual(lines, [])
        self.assertIn("future", out.stdout)
        os.utime(raw, (0, 0))
        with (self.path(rec["run_id"]) / "run.json").open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
        out, lines = self.preview()
        self.assertEqual(lines, [])
        self.assertIn("run.json too large", out.stdout)

    def test_custom_reports_root_refused_without_writes(self):
        self.create(mode="normal")
        _, lines = self.preview()
        before = tree_snapshot(self.home)
        with mock.patch.dict(os.environ, {"COPILOT_SESSION_REPORTS_DIR": str(self.home / "custom")}):
            for args, data in ((("preview",), ""), (("apply",), self.select(lines))):
                out = self.command(*args, data=data)
                self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
                self.assertIn("custom reports root", out.stderr)
        self.assertEqual(tree_snapshot(self.home), before)

    def test_apply_accepts_exact_candidate_lines_advertised_by_preview(self):
        self.create(mode="normal")
        _, lines = self.preview()
        result = self.command("apply", data="\n".join(lines) + "\n")
        self.assert_ok(result)
        self.assertIn("Moved: 1", result.stdout)

    def test_invalid_selection_rejects_entire_batch_before_creating_anything(self):
        self.create(mode="normal")
        _, lines = self.preview()
        valid = self.select(lines)
        recent = valid.rstrip("\n") + "\tinclude-recent\n"
        too_many = "".join("CANDIDATE\t%s\t%s\n" % (uuid.uuid4(), FP) for _ in range(101))
        cases = [
            (valid + "CANDIDATE\tgarbage\n", False, "malformed candidate line"),
            (valid * 2, False, "duplicate candidate"),
            (recent, False, "must use the same option"),
            (valid, True, "must use the same option"),
            (too_many, False, "at most 100 runs"),
            ("x" * (64 * 1024 + 1), False, "too much input"),
            ("CANDIDATE\t../outside\t%s\n" % FP, False, "malformed candidate line"),
            (valid.replace("\n", "\r\n"), False, "malformed candidate line"),
        ]
        before = tree_snapshot(self.home)
        for data, include_recent, reason in cases:
            with self.subTest(data=data[:80], recent=include_recent):
                result = self.command("apply", *(["--include-recent"] if include_recent else []), data=data)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("nothing was moved", result.stderr.lower())
                self.assertIn(reason, result.stderr)
                self.assertEqual(tree_snapshot(self.home), before)

    def test_changed_since_preview_and_final_capacity_recheck_move_nothing(self):
        rec = self.create()
        _, lines = self.preview()
        raw = self.path(rec["run_id"]) / "raw/private-tee.txt"
        raw.write_text("changed after preview")
        result = self.apply_lines(lines)
        self.assert_ok(result)
        self.assertIn("changed since the preview", result.stdout)
        self.assertFalse(self.quarantine.exists())
        _, lines = self.preview()
        real_lock = self.cleanup.acquire_lock
        def lock_then_change(dir_fd, name, wait):
            fd = real_lock(dir_fd, name, wait)
            if name == ".capacity.lock":
                raw.write_text("changed under capacity lock")
            return fd
        with mock.patch.object(self.cleanup, "acquire_lock", side_effect=lock_then_change):
            result = self.apply_lines(lines)
        self.assert_ok(result)
        self.assertIn("Moved: 0", result.stdout)
        self.assertIn("changed since the preview", result.stdout)
        self.assertTrue(self.path(rec["run_id"]).exists())
        self.assertTrue(self.target(rec).exists())


class TestPathsAndActivity(CleanupHome):
    def test_symlink_hardlink_fifo_and_writable_entries_are_never_moved(self):
        seed = self.create(mode="normal")
        base = self.path(seed["run_id"])
        outside = self.home / "outside-private"
        outside.write_bytes(b"DO_NOT_TOUCH")
        cases = ("symlink", "hardlink", "fifo", "writable-file", "writable-dir", "owner")
        for kind in cases:
            with self.subTest(kind=kind):
                bad = base / "unsafe"
                if kind == "symlink":
                    bad.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, bad)
                elif kind == "fifo":
                    os.mkfifo(bad, 0o600)
                elif kind == "writable-dir":
                    bad.mkdir()
                    bad.chmod(0o777)
                else:
                    bad.write_bytes(b"fixture")
                    if kind == "writable-file":
                        bad.chmod(0o666)
                # Simulated foreign ownership: never chown real paths/root.
                real_stat = self.cleanup.os.stat
                def foreign_stat(name, *args, **kwargs):
                    st = real_stat(name, *args, **kwargs)
                    if name == "unsafe":
                        values = list(st)
                        values[4] = os.getuid() + 1
                        return os.stat_result(values)
                    return st
                patch = (mock.patch.object(self.cleanup.os, "stat", side_effect=foreign_stat)
                         if kind == "owner" else contextlib.nullcontext())
                with patch:
                    out, lines = self.preview()
                self.assertEqual(lines, [], out.stdout)
                self.assertIn("unsafe folder or file", out.stdout)
                self.assertEqual(outside.read_bytes(), b"DO_NOT_TOUCH")
                if bad.is_dir() and not bad.is_symlink():
                    bad.rmdir()
                else:
                    bad.unlink()

    def test_symlink_ancestors_and_unsafe_quarantine_refuse_apply_without_following(self):
        rec = self.create(mode="normal")
        _, lines = self.preview()
        outside = self.home / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside")
        before = tree_snapshot(outside)
        self.quarantine.symlink_to(outside, target_is_directory=True)
        snapshot = tree_snapshot(self.home)
        # Whether or not the planner reports this destination, it is read-only.
        self.command("preview")
        self.assertEqual(tree_snapshot(self.home), snapshot)
        out = self.apply_lines(lines)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(tree_snapshot(outside), before)
        self.assertTrue(self.path(rec["run_id"]).exists())
        self.quarantine.unlink()
        self.quarantine.mkdir(mode=0o755)
        self.quarantine.chmod(0o755)  # main() deliberately sets umask 077.
        snapshot = tree_snapshot(self.home)
        self.command("preview")
        self.assertEqual(tree_snapshot(self.home), snapshot)
        out = self.apply_lines(lines)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must be private", out.stderr)
        self.assertEqual(list(self.quarantine.iterdir()), [])
        self.quarantine.rmdir()
        reports = self.home / ".copilot/session-reports"
        hidden = self.home / "reports-hidden"
        reports.rename(hidden)
        reports.symlink_to(hidden, target_is_directory=True)
        snapshot = tree_snapshot(hidden)
        out = self.command("preview")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(tree_snapshot(hidden), snapshot)

    def test_open_unknown_missing_failing_lsof_are_fail_closed_and_paths_complete(self):
        rec = self.create()
        base = self.path(rec["run_id"])
        for index in range(205):
            report_fixture.write(base / ("raw/many/%03d" % index), "")
        _, lines = self.preview()
        self.assertEqual(len(lines), 1)
        requested = {path for argv, _ in self.lsof_calls for path in argv[7:]}
        expected = {str(base), *(str(p) for p in base.rglob("*")), str(self.target(rec))}
        self.assertEqual(requested, expected)
        self.assertTrue(all(len(argv) - 7 <= 200 and 0 < timeout <= 15
                            for argv, timeout in self.lsof_calls))
        results = [
            subprocess.CompletedProcess([], 0, b"p999999\nf12\ni100\n", b""),
            subprocess.CompletedProcess([], 1, b"", b"permission denied"),
            subprocess.CompletedProcess([], 2, b"", b""),
            subprocess.CompletedProcess([], 0, b"unexpected", b""),
            subprocess.TimeoutExpired("lsof", 15),
            OSError(errno.EACCES, "fixture denied"),
        ]
        for result in results:
            with self.subTest(result=repr(result)):
                kwargs = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
                with mock.patch.object(self.cleanup, "run_lsof", **kwargs):
                    out, lines = self.preview()
                self.assertEqual(lines, [], out.stdout)
                self.assertTrue("open by a running process" in out.stdout or "activity check" in out.stdout)
        with mock.patch.object(self.cleanup, "find_lsof", return_value=None):
            out, lines = self.preview()
            self.assertEqual(lines, [])
            self.assertIn("no system lsof", out.stdout)

    def test_only_exact_own_run_lock_fact_is_exempt(self):
        rec = self.create(mode="normal")
        _, lines = self.preview()
        real_probe = self.cleanup.open_state
        seen = []
        def probe(paths, exempt=frozenset(), deadline=None):
            seen.append(exempt)
            self.assertEqual(len(exempt), 1)
            pid, fd, inode = next(iter(exempt))
            own = "p%d\nf%s\ni%d\n" % (pid, fd, inode)
            raw_path = self.path(rec["run_id"]) / "raw/private-tee.txt"
            with raw_path.open("rb") as raw:
                # Even with exit 1, an actual own-PID raw descriptor is activity,
                # never covered by the exemption for the verified run-lock FD.
                own_raw = "p%d\nf%d\ni%d\n" % (pid, raw.fileno(), os.fstat(raw.fileno()).st_ino)
                for code in (0, 1):
                    with self.subTest(lsof_exit=code):
                        result = subprocess.CompletedProcess([], code, own.encode(), b"")
                        with mock.patch.object(self.cleanup, "run_lsof", return_value=result):
                            self.assertEqual(real_probe(paths, exempt, deadline), "closed")
                        for unexpected in (
                            "p%d\nf%s\ni%d\n" % (pid, fd, inode + 1),
                            own_raw,
                            "p%d\nf%s\ni%d\n" % (pid + 1, fd, inode),
                            "p%d\nf%s\n" % (pid, fd),  # Missing inode is not exempt.
                        ):
                            with mock.patch.object(self.cleanup, "run_lsof",
                                                   return_value=subprocess.CompletedProcess(
                                                       [], code, (own + unexpected).encode(), b"")):
                                self.assertEqual(real_probe(paths, exempt, deadline), "open")
            return "closed"
        with mock.patch.object(self.cleanup, "open_state", side_effect=probe):
            self.assert_ok(self.apply_lines(lines))
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.path(rec["run_id"]).exists())

    def test_system_lsof_closed_foreign_open_and_actual_apply_on_owned_paths(self):
        # The only nonstubbed lsof test. Never use cwd-wide or global queries.
        # A permission-denied/unknown result stops this test with its exact
        # diagnostic, without retrying the denied activity operation.
        self.find_patch.stop()
        self.lsof_patch.stop()
        real_run = self.cleanup.run_lsof
        diagnostics = []
        def capture(argv, timeout):
            result = real_run(argv, timeout)
            diagnostics.append((result.returncode, result.stdout, result.stderr))
            return result
        def known(state, stage):
            if not state.startswith("unknown"):
                return
            diagnostic = "%s; %r" % (state, diagnostics)
            denied = ("permission denied", "operation not permitted")
            if any(word in diagnostic.lower() for word in denied):
                self.skipTest("system lsof %s denied (not retried): %s" % (stage, diagnostic))
            self.fail("system lsof %s unexpectedly inconclusive: %s" % (stage, diagnostic))
        rec = self.create(mode="normal")
        raw = self.path(rec["run_id"]) / "raw/private-tee.txt"
        with mock.patch.object(self.cleanup, "run_lsof", side_effect=capture):
            state = self.cleanup.open_state([str(raw)])
            known(state, "closed-file")
            self.assertEqual(state, "closed", diagnostics)
            proc = self.holder(raw)
            state = self.cleanup.open_state([str(raw)])
            known(state, "foreign-open")
            self.assertEqual(state, "open", diagnostics)
            out, lines = self.preview()
            if "activity check unknown" in out.stdout:
                known("unknown: " + out.stdout, "full-tree")
            self.assertEqual(lines, [], out.stdout)
            self.assertIn("open by a running process", out.stdout)
            self.stop_child(proc)
            out, lines = self.preview()
            if "activity check unknown" in out.stdout:
                known("unknown: " + out.stdout, "closed-tree")
            self.assertEqual(len(lines), 1, out.stdout + repr(diagnostics))
            result = self.apply_lines(lines)
            if "activity check unknown" in result.stdout:
                known("unknown: " + result.stdout, "apply")
            self.assert_ok(result)
            self.assertIn("Moved: 1", result.stdout, repr(diagnostics))

    def test_tree_depth_read_and_time_budgets_fail_closed_without_waiting(self):
        rec = self.create(mode="normal")
        base = self.path(rec["run_id"])
        deep = base / "raw"
        for _ in range(16):
            deep /= "nested"
            deep.mkdir()
        out, lines = self.preview()
        self.assertEqual(lines, [])
        self.assertIn("nesting too deep", out.stdout)
        shutil.rmtree(base / "raw/nested")
        with mock.patch.object(self.cleanup, "READ_BUDGET_BYTES", 1):
            out, lines = self.preview()
            self.assertEqual(lines, [])
            self.assertIn("read budget", out.stdout)
        with mock.patch.object(self.cleanup, "PREVIEW_TIME_BUDGET_SECONDS", -1):
            out, lines = self.preview()
            self.assertEqual(lines, [])
            self.assertIn("time or read budget", out.stdout)


class TestMovesRecoveryAndRestore(CleanupHome):
    def test_real_batch_roundtrip_preserves_bytes_inodes_privacy_and_outside_data(self):
        normal = self.create(mode="normal")
        rtk = self.create(1)
        outside = []
        for name in (".local/share/rtk/history.db", ".config/rtk/config.toml",
                     ".copilot/logs/native.log", ".copilot/session-state/native/events.jsonl",
                     ".copilot/otel/external.jsonl"):
            path = report_fixture.write(self.home / name, "PRIVATE_OUTSIDE_SENTINEL")
            outside.append((path, tree_snapshot(path)))
        orphan = report_fixture.write(self.publications / (str(uuid.uuid4()) + ".json"), "{}")
        outside.append((orphan, tree_snapshot(orphan)))
        before = {rec["run_id"]: tree_snapshot(self.path(rec["run_id"])) for rec in (normal, rtk)}
        pub_before = tree_snapshot(self.target(rtk))
        _, lines = self.preview()
        transitions = []
        real_save = self.cleanup.save_manifest
        def save(fd, manifest):
            transitions.append(copy.deepcopy(manifest))
            for entry in manifest["entries"]:
                if entry["state"] == "pending":
                    self.assertTrue(self.path(entry["id"]).exists())
                if entry["state"] == "pub_moved":
                    self.assertTrue(self.path(entry["id"]).exists())
                    self.assertFalse((self.publications / (entry["id"] + ".json")).exists())
            return real_save(fd, manifest)
        with mock.patch.object(self.cleanup, "save_manifest", side_effect=save):
            result = self.apply_lines(lines)
        self.assert_ok(result)
        self.assertIn("Moved: 2", result.stdout)
        batch, manifest = self.batch()
        for rec in (normal, rtk):
            self.assertFalse(self.path(rec["run_id"]).exists())
            self.assertEqual(tree_snapshot(batch / "runs" / rec["run_id"]), before[rec["run_id"]])
        self.assertEqual(tree_snapshot(batch / "publications" / self.target(rtk).name), pub_before)
        states = [m["entries"][1]["state"] for m in transitions]
        self.assertLess(states.index("pending"), states.index("pub_moved"))
        self.assertLess(states.index("pub_moved"), states.index("moved"))
        text = (batch / "manifest.json").read_text()
        for private in ("PRIVATE_TEE_SENTINEL", "PRIVATE_COMMAND_SENTINEL", "PRIVATE_PROMPT_SENTINEL",
                        "history.db", str(self.home)):
            self.assertNotIn(private, text)
        self.assertLess(len(text), 10000)
        for path in (self.quarantine, batch, batch / "runs", batch / "publications"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((batch / "manifest.json").stat().st_mode), 0o600)
        listing = self.command("list")
        self.assert_ok(listing)
        self.assertIn("BATCH\t" + batch.name, listing.stdout)
        readonly = tree_snapshot(self.home)
        plan = self.command("restore", "--batch", batch.name)
        self.assert_ok(plan)
        self.assertIn("RESTORABLE\t2", plan.stdout)
        self.assertEqual(tree_snapshot(self.home), readonly)
        renames = []
        real_rename = self.cleanup.rename_noreplace_at
        def rename(sfd, src, dfd, dst):
            renames.append(src)
            return real_rename(sfd, src, dfd, dst)
        with mock.patch.object(self.cleanup, "rename_noreplace_at", side_effect=rename):
            self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))
        self.assertLess(renames.index(rtk["run_id"]), renames.index(rtk["run_id"] + ".json"))
        for rec in (normal, rtk):
            self.assertEqual(tree_snapshot(self.path(rec["run_id"])), before[rec["run_id"]])
        self.assertEqual(tree_snapshot(self.target(rtk)), pub_before)
        for path, snapshot in outside:
            self.assertEqual(tree_snapshot(path), snapshot)

    def test_exdev_does_not_copy_or_remove_originals(self):
        rec = self.create()
        _, lines = self.preview()
        before_run = tree_snapshot(self.path(rec["run_id"]))
        before_pub = tree_snapshot(self.target(rec))
        with mock.patch.object(self.cleanup, "rename_noreplace_at",
                               side_effect=OSError(errno.EXDEV, "fixture cross-device")):
            result = self.apply_lines(lines)
        self.assert_ok(result)
        self.assertIn("different filesystem", result.stdout)
        self.assertEqual(tree_snapshot(self.path(rec["run_id"])), before_run)
        self.assertEqual(tree_snapshot(self.target(rec)), before_pub)
        batch, _ = self.batch()
        self.assertEqual(list((batch / "runs").iterdir()), [])
        self.assertEqual(list((batch / "publications").iterdir()), [])

    def test_manifest_write_failure_before_move_leaves_canonical_data(self):
        rec = self.create()
        _, lines = self.preview()
        real_write = self.cleanup.write_file_atomic_at
        def fail_pending(fd, name, data, *args, **kwargs):
            if json.loads(data)["entries"][0]["state"] == "pending":
                raise OSError(errno.ENOSPC, "fixture manifest disk full")
            return real_write(fd, name, data, *args, **kwargs)
        with mock.patch.object(self.cleanup, "write_file_atomic_at", side_effect=fail_pending):
            result = self.apply_lines(lines)
        self.assertEqual(result.returncode, 1)
        self.assertIn("manifest update failed", result.stdout)
        self.assertTrue(self.path(rec["run_id"]).exists())
        self.assertTrue(self.target(rec).exists())
        _, manifest = self.batch()
        self.assertEqual(manifest["entries"][0]["state"], "requested")

    def test_crash_after_publication_or_run_rename_recovers_actual_locations(self):
        for index, crash_after in enumerate(("publication", "run")):
            with self.subTest(crash_after=crash_after):
                rec = self.create(index)
                _, lines = self.preview()
                real_rename = self.cleanup.rename_noreplace_at
                def rename_then_die(sfd, src, dfd, dst):
                    real_rename(sfd, src, dfd, dst)
                    if src == rec["run_id"] + (".json" if crash_after == "publication" else ""):
                        raise FixtureCrash()
                with mock.patch.object(self.cleanup, "rename_noreplace_at", side_effect=rename_then_die):
                    with self.assertRaises(FixtureCrash):
                        self.apply_lines(lines)
                paths = sorted(self.quarantine.glob("*/manifest.json"))
                path = next(p for p in paths if any(
                    entry["id"] == rec["run_id"] for entry in json.loads(p.read_bytes())["entries"]))
                manifest = json.loads(path.read_bytes())
                entry = next(e for e in manifest["entries"] if e["id"] == rec["run_id"])
                self.assertEqual(entry["state"], "pending" if crash_after == "publication" else "pub_moved")
                # Fresh module models restart; status alone must not drive recovery.
                restarted = load_cleanup()
                with mock.patch.object(self, "cleanup", restarted), \
                        mock.patch.object(restarted, "now_epoch", return_value=self.cleanup_now), \
                        mock.patch.object(restarted, "run_lsof", side_effect=self.closed_lsof), \
                        mock.patch.object(restarted, "find_lsof", return_value="/usr/sbin/lsof"):
                    plan = self.command("restore", "--batch", path.parent.name)
                    self.assertIn("will restore " + rec["run_id"], plan.stdout)
                    self.assert_ok(self.command("restore", "--batch", path.parent.name, "--yes"))
                self.assertTrue(self.path(rec["run_id"]).exists())
                self.assertTrue(self.target(rec).exists())
                # Keep the second preview restricted to its own newly created run.
                self.path(rec["run_id"]).rename(self.home / ("already-restored-%d" % index))
                self.target(rec).unlink()

    def test_manifest_save_after_move_rolls_back_and_restart_is_idempotent(self):
        rec = self.create()
        _, lines = self.preview()
        run_before = tree_snapshot(self.path(rec["run_id"]))
        pub_before = tree_snapshot(self.target(rec))
        real_save = self.cleanup.save_manifest
        def fail_moved(fd, manifest):
            if manifest["entries"][0]["state"] == "moved":
                raise self.cleanup.ManifestError("fixture failed moved save")
            return real_save(fd, manifest)
        with mock.patch.object(self.cleanup, "save_manifest", side_effect=fail_moved):
            result = self.apply_lines(lines)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(tree_snapshot(self.path(rec["run_id"])), run_before)
        self.assertEqual(tree_snapshot(self.target(rec)), pub_before)
        batch, manifest = self.batch()
        self.assertEqual(manifest["entries"][0]["state"], "pub_moved")
        self.assertIn("RESTORABLE\t0", self.command("restore", "--batch", batch.name).stdout)
        self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))

    def test_rollback_collision_preserves_quarantined_publication_and_conflicting_destination(self):
        rec = self.create()
        pub_bytes = self.target(rec).read_bytes()
        _, lines = self.preview()
        real_rename = self.cleanup.rename_noreplace_at
        def fail_run(sfd, src, dfd, dst):
            if src == rec["run_id"]:
                self.target(rec).write_bytes(b"DESTINATION_COLLISION")
                raise OSError(errno.EXDEV, "fixture run rename failure")
            return real_rename(sfd, src, dfd, dst)
        with mock.patch.object(self.cleanup, "rename_noreplace_at", side_effect=fail_run):
            result = self.apply_lines(lines)
        self.assertEqual(result.returncode, 1)
        batch, manifest = self.batch()
        self.assertEqual(manifest["entries"][0]["state"], "failed")
        self.assertEqual((batch / "publications" / self.target(rec).name).read_bytes(), pub_bytes)
        self.assertEqual(self.target(rec).read_bytes(), b"DESTINATION_COLLISION")
        plan = self.command("restore", "--batch", batch.name)
        self.assertIn("already exists", plan.stdout)
        self.assertIn("RESTORABLE\t0", plan.stdout)

    def test_changed_quarantine_and_destination_collision_are_not_overwritten(self):
        rec, batch, _ = self.moved()
        raw = batch / "runs" / rec["run_id"] / "raw/private-tee.txt"
        raw.write_text("CHANGED_QUARANTINE")
        out = self.command("restore", "--batch", batch.name, "--yes")
        self.assertEqual(out.returncode, 1)
        self.assertIn("changed", out.stdout)
        self.assertFalse(self.path(rec["run_id"]).exists())
        # A canonical collision must also block without merging either tree.
        self.path(rec["run_id"]).mkdir()
        report_fixture.write(self.path(rec["run_id"]) / "sentinel", "DESTINATION")
        before = tree_snapshot(self.home)
        out = self.command("restore", "--batch", batch.name, "--yes")
        self.assertEqual(out.returncode, 1)
        self.assertIn("already exists", out.stdout)
        self.assertEqual(tree_snapshot(self.home), before)

    def test_restore_capacity_is_partial_no_eviction_then_publication_only_retry(self):
        rec, batch, _ = self.moved()
        for index in range(1, 257):
            report_fixture.write(self.publications / (export_fixture.IDS[index] + ".json"),
                                 "capacity-sentinel-%d" % index)
        before = tree_snapshot(self.publications)
        out = self.command("restore", "--batch", batch.name, "--yes")
        self.assertEqual(out.returncode, 1)
        self.assertIn("publication folder full", out.stdout)
        self.assertTrue(self.path(rec["run_id"]).exists())
        self.assertFalse(self.target(rec).exists())
        self.assertTrue((batch / "publications" / self.target(rec).name).exists())
        self.assertEqual(tree_snapshot(self.publications), before)
        (self.publications / (export_fixture.IDS[1] + ".json")).unlink()
        self.assertIn("publication only", self.command("restore", "--batch", batch.name).stdout)
        self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))
        self.assertTrue(self.target(rec).exists())
        self.assertEqual(len(list(self.publications.glob("*.json"))), 256)

    def test_manifest_untrusted_paths_ignored_and_traversal_id_refused(self):
        rec, batch, manifest = self.moved()
        sentinel = report_fixture.write(self.home / "unrelated-owned-fixture", "LEAVE_ME")
        entry = manifest["entries"][0]
        entry.update(source=str(sentinel), destination=str(sentinel), state="restored")
        path = batch / "manifest.json"
        path.write_text(json.dumps(manifest))
        self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))
        self.assertTrue(self.path(rec["run_id"]).exists())
        self.assertEqual(sentinel.read_text(), "LEAVE_ME")
        entry["id"] = "../unrelated-owned-fixture"
        path.write_text(json.dumps(manifest))
        before = tree_snapshot(self.home)
        result = self.command("restore", "--batch", batch.name, "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a valid", result.stderr)
        self.assertEqual(tree_snapshot(self.home), before)


class TestLocksAndProducerRaces(CleanupHome):
    def test_lock_order_and_foreign_run_lock_contention(self):
        rec = self.create()
        _, lines = self.preview()
        order = []
        real_lock = self.cleanup.acquire_lock
        def lock(fd, name, wait):
            order.append((name, wait))
            return real_lock(fd, name, wait)
        with mock.patch.object(self.cleanup, "acquire_lock", side_effect=lock):
            self.assert_ok(self.apply_lines(lines))
        self.assertEqual(order, [
            (".transaction.lock", 10), (".reconcile.lock", 10), (".lock", 2), (".capacity.lock", 5)])
        batch, _ = self.batch()
        self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))
        _, lines = self.preview()
        self.holder(self.path(rec["run_id"]) / ".lock", lock=True)
        with mock.patch.object(self.cleanup, "RUN_LOCK_WAIT_SECONDS", .1):
            result = self.apply_lines(lines)
        self.assert_ok(result)
        self.assertIn("Moved: 0", result.stdout)
        self.assertIn("lock is held", result.stdout)
        self.assertTrue(self.target(rec).exists())

    def test_foreign_transaction_lock_blocks_entire_batch_bounded(self):
        self.create()
        _, lines = self.preview()
        self.quarantine.mkdir(mode=0o700)
        self.holder(self.quarantine / ".transaction.lock", lock=True)
        with mock.patch.object(self.cleanup, "TRANSACTION_LOCK_WAIT_SECONDS", .1):
            result = self.apply_lines(lines)
        self.assertEqual(result.returncode, 1)
        self.assertIn("busy", result.stderr)
        self.assertEqual(list(self.quarantine.glob("*/manifest.json")), [])

    def test_stale_reconcile_probe_does_not_resurrect_quarantined_run(self):
        rec = self.create()
        _, lines = self.preview()
        real_probe = self.mod._probe_run_folder
        applied = []
        def probe(fd, name):
            result = real_probe(fd, name)
            if name == rec["run_id"] and not applied:
                applied.append(self.apply_lines(lines))
            return result
        with mock.patch.object(self.mod, "_probe_run_folder", side_effect=probe):
            self.mod.reconcile_publications()
        self.assertEqual(len(applied), 1)
        self.assert_ok(applied[0])
        self.assertFalse(self.path(rec["run_id"]).exists())
        self.assertFalse(self.target(rec).exists())
        batch, _ = self.batch()
        self.assertTrue((batch / "runs" / rec["run_id"]).exists())
        self.assertTrue((batch / "publications" / self.target(rec).name).exists())

    def test_reconcile_relists_under_lock_after_restore_not_stale_known_names(self):
        rec, batch, _ = self.moved()
        pub = (batch / "publications" / self.target(rec).name).read_bytes()
        real_list = self.mod._list_run_names
        listings = []
        def list_then_restore(fd):
            result = real_list(fd)
            listings.append(result)
            if len(listings) == 1:
                self.assertNotIn(rec["run_id"], result[0])
                self.assert_ok(self.command("restore", "--batch", batch.name, "--yes"))
            return result
        with mock.patch.object(self.mod, "_list_run_names", side_effect=list_then_restore):
            counts = self.mod.reconcile_publications()
        self.assertGreaterEqual(len(listings), 2)
        self.assertIn(rec["run_id"], listings[1][0])
        self.assertEqual(counts["withdrawn"], 0)
        self.assertEqual(self.target(rec).read_bytes(), pub)

    def test_waiting_finish_reloads_canonical_path_after_quarantine(self):
        rec = self.create(mode="normal")
        _, lines = self.preview()
        run = self.path(rec["run_id"])
        fd = os.open(run / ".lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Child uses the real producer's run_lock. Its first contended sleep
        # proves it has opened the old lock inode before quarantine happens.
        code = (
            "import importlib.util,sys,time\n"
            "spec=importlib.util.spec_from_file_location('producer',sys.argv[1])\n"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
            "real_sleep=m.time.sleep\n"
            "def sleep(t):\n print('WAITING',flush=True);real_sleep(t)\n"
            "m.time.sleep=sleep\n"
            "rc=m.main(['finish','--run-id',sys.argv[2],'--exit-code','0'])\n"
            "print('PRODUCER_STATUS',rc,flush=True)\n"
        )
        proc = self.child(code, report_fixture.HELPER, rec["run_id"])
        self.assertEqual(proc.stdout.readline().strip(), "WAITING")
        # The parent already holds precisely this run's real lock; return a
        # duplicate to cleanup rather than racing the waiting producer for it.
        with mock.patch.object(self.cleanup, "acquire_run_lock", return_value=os.dup(fd)):
            self.assert_ok(self.apply_lines(lines))
        fcntl.flock(fd, fcntl.LOCK_UN)
        stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        self.assertIn("PRODUCER_STATUS 1", stdout)
        self.assertIn("run record not found", stderr)
        self.assertFalse(run.exists())
        self.assertFalse(self.target(rec).exists())
        batch, _ = self.batch()
        self.assertTrue((batch / "runs" / rec["run_id"] / "run.json").exists())


class TestReportCleanupMenu(menu_fixture.BaseHomeTestCase):
    """Strict prompt-driven PTY tests, no implicit new-session selection."""
    _scripted_tty = menu_fixture.TestModelRefreshMenu._scripted_tty
    _session = menu_fixture.TestContextMetricsSessionManager._session
    EMPTY = "Start a new session? [Enter/n = new, p, r, q]:"
    SELECT = "Select session to resume"
    SUB = "Choose [1, 2, 3, Enter/b = back]:"
    RECENT = "Also include run reports finalized within the last 24h?"
    CONFIRM = "anything else cancels):"

    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.home, "worktree")
        self.state_dir = os.path.join(self.home, ".copilot/session-state")
        self.fake_bin = os.path.join(self.home, "fake-bin")
        for path in (self.cwd, self.state_dir, self.fake_bin):
            os.makedirs(path, exist_ok=True)
        self.copilot_log = os.path.join(self.home, "forbidden-copilot-call")
        report_fixture.write(Path(self.fake_bin) / "copilot",
                             "#!/bin/sh\nprintf called >> \"$COPILOT_LOG\"\nexit 95\n", True)
        (Path(self.fake_bin) / "python3").symlink_to(sys.executable)
        self.log = Path(self.home) / "cleanup-calls.jsonl"
        self.helper = Path(self.home) / "fake-report-cleanup.py"
        report_fixture.write(self.helper, (
            "import json,os,sys\n"
            "a=sys.argv[1:]; data=sys.stdin.read() if a[0]=='apply' else ''\n"
            "with open(os.environ['CLEANUP_CALL_LOG'],'a') as f:\n"
            " f.write(json.dumps({'args':a,'stdin':data})+'\\n')\n"
            "if os.environ.get('FAIL_COMMAND')==a[0]:\n"
            " print('fixture helper failure',file=sys.stderr);sys.exit(9)\n"
            "if a[0]=='preview':\n"
            " print('ALL workspaces; private raw data; NOT freed')\n"
            " print('CANDIDATE\\t%s\\t%s'+ ('\\tinclude-recent' if '--include-recent' in a else ''))\n"
            "elif a[0]=='list': print('owned batch\\nBATCH\\t%s')\n"
            "elif a[0]=='restore' and '--yes' not in a: print('plan only\\nRESTORABLE\\t1')\n"
            "else: print('fixture action complete')\n"
        ) % (RID, FP, BATCH))
        self.task = Path(self.home) / "fake-task.py"
        report_fixture.write(self.task, "import sys\n")

    def menu_env(self, extra=None):
        additions = {
            "PATH": self.fake_bin + ":/usr/bin:/bin",
            "COPILOT_LOG": self.copilot_log, "CLEANUP_CALL_LOG": str(self.log),
            "COPILOT_REPORT_CLEANUP_HELPER": str(self.helper),
            "COPILOT_TASK_REPORT_HELPER": str(self.task),
            "GIT_CEILING_DIRECTORIES": self.home,
        }
        additions.update(extra or {})
        return self.env(additions)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def clean_log(self):
        if self.log.exists():
            self.log.unlink()

    def no_launch(self, status, out):
        self.assertEqual(status, 0, out)
        self.assertFalse(os.path.exists(self.copilot_log), out)
        self.assertNotIn("Starting copilot...", out)
        self.assertNotIn("Session name (Enter for automatic):", out)

    def test_both_menu_variants_r_R_preview_apply_list_restore_back_never_launch(self):
        for regular, recent in ((False, False), (True, True)):
            with self.subTest(regular=regular, recent=recent):
                if regular:
                    self._session("kept", name="Saved fixture")
                self.clean_log()
                prompt = self.SELECT if regular else self.EMPTY
                status, out = self._scripted_tty(self.menu_env(), [
                    (prompt, "R\n" if regular else "r\n"), (self.SUB, "1\n"),
                    (self.RECENT, "y\n" if recent else "\n"), (self.CONFIRM, "yes\n"),
                    (self.SUB, "2\n"), (self.SUB, "3\n"),
                    ("Batch to restore", "1\n"), (self.CONFIRM, "yes\n"),
                    (self.SUB, "b\n"), (prompt, "q\n"),
                ])
                self.no_launch(status, out)
                policy = ["--include-recent"] if recent else []
                selected = "CANDIDATE\t%s\t%s%s\n" % (
                    RID, FP, "\tinclude-recent" if recent else "")
                self.assertEqual(self.calls(), [
                    {"args": ["preview", *policy], "stdin": ""},
                    {"args": ["apply", *policy], "stdin": selected},
                    {"args": ["list"], "stdin": ""}, {"args": ["list"], "stdin": ""},
                    {"args": ["restore", "--batch", BATCH], "stdin": ""},
                    {"args": ["restore", "--batch", BATCH, "--yes"], "stdin": ""},
                ])
                self.assertIn("Clean run reports and private RTK data (recoverable)", out)
                self.assertIn("ALL workspaces", out)
                for marker in ("CANDIDATE\t", "BATCH\t", "RESTORABLE\t"):
                    self.assertNotIn(marker, out)

    def test_nonliteral_confirmations_and_back_default_never_apply(self):
        for answer in ("\n", "y\n", "YES\n", " yes\n", "yes \n", "\x04"):
            with self.subTest(answer=repr(answer)):
                self.clean_log()
                status, out = self._scripted_tty(self.menu_env(), [
                    (self.EMPTY, "r\n"), (self.SUB, "1\n"), (self.RECENT, "\n"),
                    (self.CONFIRM, answer), (self.SUB, "\n"), (self.EMPTY, "q\n"),
                ])
                self.no_launch(status, out)
                self.assertEqual([c["args"] for c in self.calls()], [["preview"]])
                self.assertIn("Cancelled", out)

    def test_restore_cancel_invalid_batch_choice_and_nonliteral_yes(self):
        for pick, confirm in (("\n", None), ("0\n", None), ("2\n", None),
                              ("1\n", "YES\n"), ("1\n", "yes \n")):
            with self.subTest(pick=pick, confirm=confirm):
                self.clean_log()
                actions = [(self.EMPTY, "r\n"), (self.SUB, "3\n"), ("Batch to restore", pick)]
                if confirm is not None:
                    actions.append((self.CONFIRM, confirm))
                actions += [(self.SUB, "b\n"), (self.EMPTY, "q\n")]
                status, out = self._scripted_tty(self.menu_env(), actions)
                self.no_launch(status, out)
                self.assertFalse(any("--yes" in c["args"] for c in self.calls()))

    def test_missing_and_failing_helpers_return_to_usable_menu(self):
        status, out = self._scripted_tty(self.menu_env({
            "COPILOT_REPORT_CLEANUP_HELPER": os.path.join(self.home, "missing.py")}), [
                (self.EMPTY, "r\n"), ("Press Enter to return", "\n"), (self.EMPTY, "q\n")])
        self.no_launch(status, out)
        self.assertIn("helper not found", out)
        for failure in ("preview", "apply", "list", "restore"):
            with self.subTest(failure=failure):
                self.clean_log()
                actions = [(self.EMPTY, "r\n")]
                if failure in ("preview", "apply"):
                    actions += [(self.SUB, "1\n"), (self.RECENT, "\n")]
                    if failure == "apply":
                        actions += [(self.CONFIRM, "yes\n")]
                elif failure == "list":
                    actions += [(self.SUB, "2\n")]
                else:
                    actions += [(self.SUB, "3\n"), ("Batch to restore", "1\n")]
                actions += [(self.SUB, "b\n"), (self.EMPTY, "q\n")]
                status, out = self._scripted_tty(self.menu_env({"FAIL_COMMAND": failure}), actions)
                self.no_launch(status, out)
                self.assertIn("exit 9", out)

    def test_existing_p_c_and_explicit_n_still_dispatch_separately(self):
        refresh = Path(self.home) / "fake-refresh.py"
        report_fixture.write(refresh, "print('Final effective routing (kept): fixture')\nraise SystemExit(3)\n")
        status, out = self._scripted_tty(self.menu_env({
            "COPILOT_MODEL_REFRESH_HELPER": str(refresh)}), [
                (self.EMPTY, "p\n"), ("Press Enter to return", "\n"), (self.EMPTY, "q\n")])
        self.no_launch(status, out)
        self.assertEqual(self.calls(), [])
        self._session("kept", name="Keep this")
        old_cleanup = Path(self.home) / "fake-session-cleanup.py"
        report_fixture.write(old_cleanup, "print('OLD_C_CLEANUP_ONLY')\n")
        status, out = self._scripted_tty(self.menu_env({
            "COPILOT_SESSION_CLEANUP_HELPER": str(old_cleanup)}), [
                (self.SELECT, "c\n"), ("Also include recent empty sessions", "\n"),
                (self.SELECT, "q\n")])
        self.no_launch(status, out)
        self.assertIn("OLD_C_CLEANUP_ONLY", out)
        self.assertEqual(self.calls(), [])
        # Explicit n is the sole launch in this test, intercepted by the fake.
        status, out = self._scripted_tty(self.menu_env(), [
            (self.SELECT, "n\n"), ("Session name (Enter for automatic):", "\n"),
            ("Task ID/name for usage tracking (Enter to skip):", "\n")])
        self.assertTrue(os.path.exists(self.copilot_log), out)
        self.assertEqual(self.calls(), [])

    def test_help_and_helper_resolution_override_sibling_and_fallback(self):
        resolver = menu_fixture.TestHelperResolution._resolved_helper_path
        self.assertEqual(resolver(self, str(ROOT / "bin/copilot-s"), variable="REPORT_CLEANUP_HELPER"),
                         str(HELPER))
        override = os.path.join(self.home, "override.py")
        self.assertEqual(resolver(self, str(ROOT / "bin/copilot-s"),
                                  {"COPILOT_REPORT_CLEANUP_HELPER": override},
                                  variable="REPORT_CLEANUP_HELPER"), override)
        standalone = Path(self.home) / "standalone/copilot-s"
        standalone.parent.mkdir()
        shutil.copyfile(ROOT / "bin/copilot-s", standalone)
        self.assertEqual(resolver(self, str(standalone), variable="REPORT_CLEANUP_HELPER"),
                         os.path.join(self.home, ".local/bin/copilot-report-cleanup.py"))
        result = menu_fixture.run([str(ROOT / "bin/copilot-s"), "--help"],
                                  env=self.menu_env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COPILOT_REPORT_CLEANUP_HELPER", result.stdout)
        self.assertIn("Clean run reports and private RTK data", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.copilot_log))


class TestReportCleanupDistribution(menu_fixture.BaseHomeTestCase):
    _resolved_helper_path = menu_fixture.TestHelperResolution._resolved_helper_path

    def test_fake_prefix_install_modes_and_uninstall_preserve_report_quarantine(self):
        # Existing installer harness pattern, confined to this test's HOME.
        quarantine = Path(self.home) / ".copilot/copilot-s-report-quarantine" / BATCH
        sentinel = report_fixture.write(quarantine / "runs" / RID / "raw/private.db", "RECOVERABLE")
        old_snapshot = tree_snapshot(quarantine)
        for copy_mode in (False, True):
            with self.subTest(copy=copy_mode):
                prefix = Path(self.home) / ("copy-bin" if copy_mode else "symlink-bin")
                args = ["--prefix", str(prefix)] + (["--copy"] if copy_mode else [])
                result = menu_fixture.run([menu_fixture.INSTALL_SH, *args], env=self.env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                helper = prefix / HELPER.name
                self.assertEqual(helper.is_symlink(), not copy_mode)
                self.assertTrue(os.access(helper, os.X_OK))
                self.assertEqual(helper.read_bytes(), HELPER.read_bytes())
                resolved = self._resolved_helper_path(str(prefix / "copilot-s"),
                                                     variable="REPORT_CLEANUP_HELPER")
                self.assertEqual(os.path.realpath(resolved),
                                 os.path.realpath(helper if copy_mode else HELPER))
                manifest = Path(self.home) / ".copilot-cli-toolkit/install-manifest.txt"
                self.assertIn(str(helper), manifest.read_text().splitlines())
                result = menu_fixture.run([menu_fixture.UNINSTALL_SH], env=self.env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(os.path.lexists(helper))
                self.assertEqual(sentinel.read_text(), "RECOVERABLE")
                self.assertEqual(tree_snapshot(quarantine), old_snapshot)
                self.assertIn("quarantined sessions and run reports", result.stdout)


if __name__ == "__main__":
    unittest.main()
