"""Permanent purge contracts; owned scratch HOME, synthetic producers, no RTK.

The shared cases run against both real cleanup adapters. Only the explicitly
named system-lsof/PTTY cases leave activity/terminal seams unpatched. Every
destructive invocation selects one exact batch created by that test.
"""
import contextlib
import copy
import errno
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import stat
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

import test_copilot_report_cleanup as reports
import test_copilot_s as menus
import test_copilot_session_cleanup as sessions


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "bin/copilot_quarantine_purge.py"
snapshot = reports.tree_snapshot


def load_engine():
    spec = importlib.util.spec_from_file_location("fresh_purge_test_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


class PurgeContracts:
    """Same destructive and refusal assertions for sessions AND reports."""

    def setUp(self):
        super().setUp()
        self.helper = sessions.cleanup if self.kind == "sessions" else self.cleanup
        self.engine = load_engine()
        patches = [
            mock.patch.object(self.helper, "load_purge_engine", return_value=self.engine),
            mock.patch.object(self.engine, "find_lsof", return_value="/usr/sbin/lsof"),
            mock.patch.object(self.engine, "run_lsof", side_effect=lambda argv, timeout:
                              subprocess.CompletedProcess(argv, 1, b"", b"")),
        ]
        self.loader_patch, self.system_find_patch, self.closed_patch = patches
        for patch in patches:
            probe = patch.start()
            if patch is self.closed_patch:
                self.closed_probe = probe
            self.addCleanup(patch.stop)
        self.cli_env = dict(self.environment if self.kind == "sessions" else self.env)
        self.cli_env["TMPDIR"] = str(self.home)
        self.cli_env["PATH"] = os.pathsep.join(
            [str(self.home / "fake-bin"), str(Path(sys.executable).parent), "/usr/bin", "/bin"])

    def command(self, *args, data="", terminal=False):
        out, err = TerminalOutput() if terminal else io.StringIO(), io.StringIO()
        argv = self.args(*args) if self.kind == "sessions" else list(args)
        with mock.patch.object(sys, "stdin", io.StringIO(data)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.helper.main(argv)
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())

    def ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def make_batch(self, index=0, sparse=False):
        previous = set(self.quarantine.glob("*/manifest.json"))
        if self.kind == "sessions":
            if index == 0:
                sid, source, payload, manifest, _ = self.moved_fixture()
                origins = [source]
            else:
                sid, source = self.session(index + 1)
                self.registry.write_text(self.row(sid))
                _, candidates = self.preview()
                self.ok(self.apply(candidates))
                manifest, = set(self.quarantine.glob("*/manifest.json")) - previous
                payload = manifest.parent / "sessions" / sid
                origins = [source]
        else:
            rec = self.create(index)
            source, publication = self.path(rec["run_id"]), self.target(rec)
            if sparse:
                path = source / "raw/sparse-fixture.bin"
                with path.open("wb") as stream:
                    stream.seek(8 * 1024 * 1024)
                    stream.write(b"SYNTHETIC_END")
            _, lines = self.preview()
            self.assertEqual(len(lines), 1)
            self.ok(self.apply_lines(lines))
            manifest, = set(self.quarantine.glob("*/manifest.json")) - previous
            sid = rec["run_id"]
            payload = manifest.parent / "runs" / sid
            origins = [source, publication]
            self.assertTrue((manifest.parent / "publications" / publication.name).is_file())
        fixture = types.SimpleNamespace(
            batch=manifest.parent, manifest=manifest, payload=payload, origins=origins, id=sid)
        self.assert_origins_empty(fixture)
        return fixture

    def assert_origins_empty(self, fixture):
        for path in fixture.origins:
            self.assertFalse(path.exists(), str(path))
        if self.kind == "sessions":
            self.assertEqual(self.registry.read_bytes(), b"")
            self.assertEqual(list(self.state.iterdir()), [])
        else:
            self.assertEqual([p for p in fixture.origins[0].parent.iterdir()
                              if not p.name.startswith(".")], [])
            self.assertEqual(list(self.publications.glob("*.json")), [])

    def origins_snapshot(self, fixture):
        if self.kind == "sessions":
            return snapshot(self.state), snapshot(self.registry)
        return snapshot(fixture.origins[0].parent), snapshot(self.publications)

    def journal(self, fixture):
        return self.quarantine / ".purge" / (fixture.batch.name + ".json")

    def preview_purge(self, fixture):
        return self.command("purge-preview", "--batch", fixture.batch.name)

    def digest(self, fixture, result=None):
        result = result or self.preview_purge(fixture)
        self.ok(result)
        line, = [line for line in result.stdout.splitlines() if line.startswith("PURGE_SNAPSHOT\t")]
        self.assertRegex(line, r"^PURGE_SNAPSHOT\t%s\t%s\t[0-9a-f]{64}$" %
                         (self.kind, fixture.batch.name))
        return line.split("\t")[3]

    def apply_purge(self, fixture, answer=True, digest=None):
        args = ["purge", "--batch", fixture.batch.name]
        if digest is not None:
            args += ["--snapshot", digest]
        with mock.patch.object(self.engine, "_open_tty", side_effect=lambda:
                               os.open(os.devnull, os.O_RDWR)), \
                mock.patch.object(self.engine, "confirm_on_tty", side_effect=(
                    answer if callable(answer) else lambda *args: answer)) as confirm:
            result = self.command(*args, terminal=True)
        return result, confirm

    def restore(self, fixture):
        args = ["restore", "--batch", fixture.batch.name]
        return self.command(*args, *(["--yes"] if self.kind == "reports" else []), data="yes\n")

    def test_list_is_manifest_only_and_preview_is_readonly_with_exact_estimate(self):
        fixture = self.make_batch(sparse=self.kind == "reports")
        neighbor = self.make_batch(1)
        before = snapshot(self.home)
        with mock.patch.object(self.engine, "take_inventory", side_effect=AssertionError(
                "purge-list must not inventory payloads")), \
                mock.patch.object(self.engine, "run_lsof", side_effect=AssertionError(
                    "purge-list must not probe activity")):
            listed = self.command("purge-list")
        self.ok(listed)
        self.assertEqual({line for line in listed.stdout.splitlines() if line.startswith("PURGEABLE\t")},
                         {"PURGEABLE\t" + f.batch.name for f in (fixture, neighbor)})
        plans, real_build = [], self.engine.build_plan
        def capture_plan(*args):
            plan = real_build(*args)
            plans.append(plan)
            return plan
        with mock.patch.object(self.engine, "build_plan", side_effect=capture_plan):
            result = self.preview_purge(fixture)
        self.digest(fixture, result)
        expected_paths = {str(p) for p in [fixture.batch, *fixture.batch.rglob("*")]}
        probed_paths = {path for call in self.closed_probe.call_args_list
                        for path in call.args[0][call.args[0].index("--") + 1:]}
        self.assertEqual(probed_paths, expected_paths)
        allocated = sum(p.lstat().st_blocks * 512
                        for p in [fixture.batch, *fixture.batch.rglob("*")])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].inventory.totals()[3], allocated)
        self.assertIn("about %s allocated on disk" % self.engine.human_bytes(allocated), result.stdout)
        self.assertIn("st_blocks x 512", result.stdout)
        self.assertIn("APFS", result.stdout)
        if self.kind == "reports":
            # Seek-written files need not be sparse on this host filesystem.
            # The exact observed st_blocks * 512 estimate above is the contract.
            self.assertIn("+publication", result.stdout)
        self.assertEqual(snapshot(self.home), before)
        legacy = self.command("list")
        self.ok(legacy)
        if self.kind == "reports":
            self.assertEqual({s for s in legacy.stdout.splitlines() if s.startswith("BATCH\t")},
                             {"BATCH\t" + f.batch.name for f in (fixture, neighbor)})
        else:
            self.assertIn(fixture.batch.name + "  moved 1", legacy.stdout)
        self.assertFalse((self.quarantine / ".purge").exists())

    def tty_purge(self, fixture, response):
        """Real controlling terminal, /dev/tty reads and terminal stdout."""
        helper = sessions.HELPER_PATH if self.kind == "sessions" else reports.HELPER
        args = ["purge", "--batch", fixture.batch.name]
        argv = self.args(*args) if self.kind == "sessions" else args
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(str(self.home))
            os.execve(sys.executable, [sys.executable, "-u", str(helper), *argv], self.cli_env)
        output, sent, status = bytearray(), False, None
        deadline = time.monotonic() + 35
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    try:
                        chunk = os.read(master, 16384)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    match = re.search(rb"\r?\n    (PURGE (?:sessions|reports) "
                                      rb"[0-9-]+[0-9a-f]{8} [0-9a-f]{12})\r?\n> ", output)
                    if match and not sent:
                        phrase = match[1].decode()
                        reply = response(phrase) if callable(response) else response
                        os.write(master, (reply + "\n").encode())
                        sent = True
                waited, child_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = os.waitstatus_to_exitcode(child_status)
                    break
            else:
                self.fail("Purge PTY deadline:\n" + output.decode(errors="replace"))
            if status is None:
                _, child_status = os.waitpid(pid, 0)
                status = os.waitstatus_to_exitcode(child_status)
        finally:
            if status is None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            os.close(master)
        return types.SimpleNamespace(
            returncode=status, stdout=output.decode(errors="replace"), stderr="", sent=sent)

    def test_system_lsof_mount_and_real_tty_purge_only_selected_batch(self):
        fixture = self.make_batch(sparse=self.kind == "reports")
        neighbor = self.make_batch(1)
        outside = self.home / "original-data-must-stay"
        outside.write_bytes(b"ORIGINAL_DATA")
        neighbor_before, origin_before = snapshot(neighbor.batch), self.origins_snapshot(fixture)
        allocated = sum(p.lstat().st_blocks * 512
                        for p in [fixture.batch, *fixture.batch.rglob("*")])
        self.closed_patch.stop()
        self.system_find_patch.stop()
        if self.engine.find_lsof() is None:
            self.skipTest("system lsof unavailable; no positive probe can be asserted")
        preview = self.preview_purge(fixture)
        self.digest(fixture, preview)
        result = self.tty_purge(fixture, lambda phrase: phrase)
        self.ok(result)
        self.assertTrue(result.sent, result.stdout)
        self.assertIn("Purged batch %s permanently" % fixture.batch.name, result.stdout)
        self.assertFalse(fixture.batch.exists())
        journal = json.loads(self.journal(fixture).read_bytes())
        self.assertEqual(journal["state"], "complete")
        self.assertEqual(journal["planned"]["allocated_bytes_estimate"], allocated)
        self.assertEqual(journal["deleted"]["files"], journal["planned"]["files"])
        self.assertEqual(journal["deleted"]["folders"], journal["planned"]["folders"])
        self.assertEqual(stat.S_IMODE(self.journal(fixture).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.journal(fixture).parent.stat().st_mode), 0o700)
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)
        self.assertEqual(self.origins_snapshot(fixture), origin_before)
        self.assert_origins_empty(fixture)
        self.assertEqual(outside.read_bytes(), b"ORIGINAL_DATA")
        self.assertEqual(self.preview_purge(fixture).returncode, 1)
        self.assertEqual(self.apply_purge(fixture)[0].returncode, 1)
        self.assertEqual(self.restore(fixture).returncode, 1)
        self.assertIn("Purged earlier: 1", self.command("purge-list").stdout)

    def test_wrong_phrase_real_tty_and_piped_stdin_never_create_journal(self):
        fixture = self.make_batch()
        before = snapshot(self.home)
        result = self.tty_purge(fixture, "yes")
        self.ok(result)
        self.assertTrue(result.sent, result.stdout)
        self.assertIn("phrase did not match exactly", result.stdout)
        self.assertEqual(snapshot(self.home), before)
        helper = sessions.HELPER_PATH if self.kind == "sessions" else reports.HELPER
        args = ["purge", "--batch", fixture.batch.name]
        argv = self.args(*args) if self.kind == "sessions" else args
        phrase = "PURGE %s %s %s\n" % (self.kind, fixture.batch.name, self.digest(fixture)[:12])
        result = subprocess.run([sys.executable, str(helper), *argv], input=phrase,
                                capture_output=True, text=True, env=self.cli_env,
                                start_new_session=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("interactive terminal", result.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.journal(fixture).exists())

    def test_timeout_and_interrupt_cancellation_are_nonmutating(self):
        fixture = self.make_batch()
        before = snapshot(self.home)
        for answer, expected in ((None, 0), (False, 0)):
            with self.subTest(answer=answer):
                result, confirm = self.apply_purge(fixture, answer)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(confirm.call_args[0][2], 120)
                self.assertEqual(snapshot(self.home), before)
        def interrupt(*args):
            raise KeyboardInterrupt
        result, _ = self.apply_purge(fixture, interrupt)
        self.assertEqual(result.returncode, 130)
        self.assertEqual(snapshot(self.home), before)

    def test_redirected_stdout_and_invalid_snapshot_refuse_before_confirmation(self):
        fixture = self.make_batch()
        before = snapshot(self.home)
        with mock.patch.object(self.engine, "_open_tty", side_effect=lambda:
                               os.open(os.devnull, os.O_RDWR)), \
                mock.patch.object(self.engine, "confirm_on_tty") as confirm:
            result = self.command("purge", "--batch", fixture.batch.name)
        self.assertEqual(result.returncode, 1)
        self.assertIn("standard output", result.stderr)
        confirm.assert_not_called()
        for digest in ("bad", "0" * 64):
            with self.subTest(digest=digest):
                result, confirm = self.apply_purge(fixture, digest=digest)
                self.assertEqual(result.returncode, 1)
                confirm.assert_not_called()
                self.assertEqual(snapshot(self.home), before)

    def test_confirmation_holds_no_lock_and_rechecks_snapshot_after_answer(self):
        fixture = self.make_batch()
        neighbor = self.make_batch(1)
        origin_before, neighbor_before = self.origins_snapshot(fixture), snapshot(neighbor.batch)
        def answer(fd, phrase, timeout):
            with self.lock_holder():
                pass  # A genuine LOCK_NB succeeds while the question is open.
            data = json.loads(fixture.manifest.read_bytes())
            data["created_at"] = "2026-01-01T00:00:00Z"
            fixture.manifest.write_text(json.dumps(data))
            return True
        result, _ = self.apply_purge(fixture, answer, self.digest(fixture))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changed after you confirmed", result.stdout)
        self.assertFalse(self.journal(fixture).exists())
        self.assertTrue(fixture.payload.is_dir())
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)
        self.assertEqual(self.origins_snapshot(fixture), origin_before)

    @contextlib.contextmanager
    def lock_holder(self):
        path = (Path(str(self.registry) + ".lock") if self.kind == "sessions" else
                self.quarantine / ".transaction.lock")
        with path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def test_real_held_lock_refuses_purge_and_restore_without_moving(self):
        fixture = self.make_batch()
        before = snapshot(self.home)
        constant = "LOCK_TIMEOUT_SECONDS" if self.kind == "sessions" else "TRANSACTION_LOCK_WAIT_SECONDS"
        with mock.patch.object(self.helper, constant, .02), self.lock_holder():
            result, _ = self.apply_purge(fixture)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("busy", (result.stdout + result.stderr).lower())
            result = self.restore(fixture)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assert_origins_empty(fixture)
        self.assertFalse(self.journal(fixture).exists())

    def test_unsettled_malformed_and_duplicate_manifests_are_ineligible(self):
        fixture = self.make_batch()
        original = json.loads(fixture.manifest.read_bytes())
        variants = []
        for state in ("pending", "failed", "restoring", "restored", "pub_moved"):
            data = copy.deepcopy(original)
            data["entries"][0]["state"] = state
            variants.append((state, data))
        data = copy.deepcopy(original)
        data["unexpected"] = True
        variants.append(("unknown field", data))
        data = copy.deepcopy(original)
        data["entries"].append(copy.deepcopy(data["entries"][0]))
        variants.append(("duplicate id", data))
        data = copy.deepcopy(original)
        data["entries"][0]["fingerprint"] = "bad"
        variants.append(("bad fingerprint", data))
        for label, data in variants:
            with self.subTest(label=label):
                fixture.manifest.write_text(json.dumps(data))
                before = snapshot(self.home)
                self.assertNotIn("PURGEABLE\t", self.command("purge-list").stdout)
                result = self.preview_purge(fixture)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("PURGE_INELIGIBLE\t" + fixture.batch.name, result.stdout)
                result, confirm = self.apply_purge(fixture)
                self.assertEqual(result.returncode, 1)
                confirm.assert_not_called()
                self.assertEqual(snapshot(self.home), before)

    def test_skipped_absent_allowed_but_skipped_present_and_unknown_objects_refused(self):
        fixture = self.make_batch()
        data = json.loads(fixture.manifest.read_bytes())
        skipped = copy.deepcopy(data["entries"][0])
        skipped.update(id="ffffffff-ffff-4fff-8fff-ffffffffffff", state="skipped")
        if self.kind == "sessions":
            skipped.update(registry=None, registry_rows=[])
        data["entries"].append(skipped)
        fixture.manifest.write_text(json.dumps(data))
        self.digest(fixture)
        container = fixture.payload.parent
        for extra in (container / skipped["id"], fixture.batch / "unexpected",
                      container / "unexpected"):
            with self.subTest(extra=str(extra.relative_to(fixture.batch))):
                extra.mkdir(mode=0o700)
                before = snapshot(self.home)
                result = self.preview_purge(fixture)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(snapshot(self.home), before)
                extra.rmdir()
        if self.kind == "reports":
            pub = fixture.batch / "publications" / (skipped["id"] + ".json")
            pub.write_text("UNRECORDED")
            self.assertEqual(self.preview_purge(fixture).returncode, 1)

    def test_symlink_hardlink_fifo_and_unsafe_modes_refuse_without_outside_open(self):
        fixture = self.make_batch()
        outside = self.home / "outside-synthetic"
        outside.write_bytes(b"DO_NOT_OPEN_OR_DELETE")
        trap = fixture.batch / "trap"
        real_open = os.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotIn(os.fspath(path), ("trap", str(outside)))
            return real_open(path, *args, **kwargs)
        for kind in ("symlink", "hardlink", "fifo", "writable", "sticky"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    trap.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, trap)
                elif kind == "fifo":
                    os.mkfifo(trap, 0o600)
                else:
                    trap.write_bytes(b"UNEXPECTED")
                    trap.chmod(0o666 if kind == "writable" else 0o1600)
                before = snapshot(self.home)
                with mock.patch.object(os, "open", side_effect=guarded_open):
                    result = self.preview_purge(fixture)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(snapshot(self.home), before)
                trap.unlink()
        self.assertEqual(outside.read_bytes(), b"DO_NOT_OPEN_OR_DELETE")
        self.assertFalse(self.journal(fixture).exists())

    def test_batch_traversal_and_symlink_never_open_outside_target(self):
        fixture = self.make_batch()
        outside = self.home / "outside-folder"
        outside.mkdir(mode=0o700)
        (outside / "sentinel").write_bytes(b"KEEP_OUTSIDE")
        alias = self.quarantine / "20261119-000000-deadbeef"
        alias.symlink_to(outside, target_is_directory=True)
        before = snapshot(self.home)
        real_open = os.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(os.fspath(path), str(outside))
            self.assertNotEqual(os.fspath(path), "sentinel")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=guarded_open):
            for name in ("../outside-folder", str(outside), fixture.batch.name + "\n", alias.name):
                with self.subTest(batch=name):
                    result = self.command("purge-preview", "--batch", name)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.journal(fixture).exists())

    def test_mount_unknown_open_lsof_and_inventory_budgets_fail_closed(self):
        fixture = self.make_batch()
        before = snapshot(self.home)
        cases = [
            ("mount crossing", mock.patch.object(
                self.engine.MountGuard, "check_dir",
                side_effect=self.engine.Refused("injected mount crossing"))),
            ("mount unknown", mock.patch.object(
                self.engine, "_darwin_mount_of" if sys.platform == "darwin" else "_linux_mount_of",
                side_effect=self.engine.PurgeError("injected unknown mount"))),
            ("open", mock.patch.object(self.engine, "run_lsof",
                                      return_value=subprocess.CompletedProcess([], 0, b"p123\n", b""))),
            ("ambiguous", mock.patch.object(self.engine, "run_lsof",
                                           return_value=subprocess.CompletedProcess([], 1, b"", b"denied"))),
            ("lsof missing", mock.patch.object(self.engine, "find_lsof", return_value=None)),
            ("lsof timeout", mock.patch.object(self.engine, "run_lsof",
                                              side_effect=subprocess.TimeoutExpired("lsof", 1))),
            ("entries", mock.patch.object(self.engine, "MAX_ENTRIES", 1)),
            ("depth", mock.patch.object(self.engine, "MAX_DEPTH", 1)),
            ("time", mock.patch.object(self.engine, "PLAN_BUDGET_SECONDS", -1)),
        ]
        for label, patch in cases:
            with self.subTest(label=label), patch:
                result = self.preview_purge(fixture)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                result, confirm = self.apply_purge(fixture)
                self.assertEqual(result.returncode, 1)
                confirm.assert_not_called()
                self.assertEqual(snapshot(self.home), before)

    def test_preview_accepts_exact_entry_limit_and_refuses_one_less_readonly(self):
        fixture = self.make_batch()
        # Count each real non-root object once. The old verification relist
        # counted child names again after the walk had already counted them.
        entries = len(list(fixture.batch.rglob("*")))
        before = snapshot(self.home)
        with mock.patch.object(self.engine, "MAX_ENTRIES", entries):
            self.digest(fixture, self.preview_purge(fixture))
        self.assertEqual(snapshot(self.home), before)
        with mock.patch.object(self.engine, "MAX_ENTRIES", entries - 1):
            result = self.preview_purge(fixture)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("more than %d entries" % (entries - 1), result.stdout)
        self.assertIn("PURGE_INELIGIBLE\t" + fixture.batch.name, result.stdout)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.journal(fixture).exists())

    def test_double_inventory_detects_mutation_during_activity_probe(self):
        fixture = self.make_batch()
        def changed(argv, timeout):
            data = json.loads(fixture.manifest.read_bytes())
            data["created_at"] = "2026-01-01T00:00:00Z"
            fixture.manifest.write_text(json.dumps(data))
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        with mock.patch.object(self.engine, "run_lsof", side_effect=changed):
            result = self.preview_purge(fixture)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changed while being inspected", result.stdout)
        self.assertTrue(fixture.payload.is_dir())
        self.assertFalse(self.journal(fixture).exists())

    def test_payload_fingerprint_change_after_answer_prevents_any_deletion(self):
        fixture = self.make_batch()
        payload = (fixture.payload / "workspace.yaml" if self.kind == "sessions" else
                   fixture.payload / "raw/private-tee.txt")
        def changed(*args):
            payload.write_bytes(payload.read_bytes() + b"\nPOST_CONFIRMATION_CHANGE")
            return True
        result, _ = self.apply_purge(fixture, changed)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("changed since it was quarantined", result.stdout)
        self.assertIn(b"POST_CONFIRMATION_CHANGE", payload.read_bytes())
        self.assertFalse(self.journal(fixture).exists())
        self.assert_origins_empty(fixture)

    def test_unreadable_started_partial_and_complete_markers_block_restore_and_retry(self):
        fixture = self.make_batch()
        control = self.quarantine / ".purge"
        control.mkdir(mode=0o700)
        for state in ("unreadable", "started", "partial", "complete"):
            with self.subTest(state=state):
                data = {"kind": self.engine.JOURNAL_KIND, "version": 1,
                        "batch": fixture.batch.name, "state": state}
                self.journal(fixture).write_text("{" if state == "unreadable" else json.dumps(data))
                self.journal(fixture).chmod(0o600)
                before = snapshot(self.home)
                self.assertEqual(self.preview_purge(fixture).returncode, 1)
                self.assertNotIn("PURGEABLE\t", self.command("purge-list").stdout)
                self.assertEqual(self.restore(fixture).returncode, 1)
                result, confirm = self.apply_purge(fixture)
                self.assertEqual(result.returncode, 1)
                confirm.assert_not_called()
                self.assertEqual(snapshot(self.home), before)

    def test_journal_write_failure_prevents_first_deletion(self):
        fixture = self.make_batch()
        before = snapshot(fixture.batch)
        with mock.patch.object(self.engine.Journal, "_write_tmp",
                               side_effect=OSError(errno.ENOSPC, "injected journal ENOSPC")):
            result, _ = self.apply_purge(fixture)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cannot write the purge journal", result.stderr)
        self.assertEqual(snapshot(fixture.batch), before)
        self.assertFalse(self.journal(fixture).exists())
        self.assert_origins_empty(fixture)

    def test_first_and_mid_delete_failure_records_durable_journal_and_stops(self):
        neighbor = self.make_batch(1)
        neighbor_before = snapshot(neighbor.batch)
        for index, fail_at in ((2, 1), (3, 2)):
            with self.subTest(fail_at=fail_at):
                fixture = self.make_batch(index)
                before = snapshot(fixture.batch)
                origin_before = self.origins_snapshot(fixture)
                deleted = []
                real_unlink = os.unlink
                def failing_unlink(path, *args, **kwargs):
                    # Journal temporary-file unlink is not a payload deletion.
                    if str(path).endswith(".tmp"):
                        return real_unlink(path, *args, **kwargs)
                    self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
                    if len(deleted) + 1 == fail_at:
                        raise OSError(errno.EIO, "injected payload EIO")
                    result = real_unlink(path, *args, **kwargs)
                    deleted.append(str(path))
                    return result
                with mock.patch.object(os, "unlink", side_effect=failing_unlink):
                    result, _ = self.apply_purge(fixture)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("PURGE STOPPED", result.stdout)
                self.assertIn("injected payload EIO", result.stdout)
                journal = json.loads(self.journal(fixture).read_bytes())
                self.assertEqual(journal["state"], "partial")
                self.assertEqual(journal["deleted"]["files"], fail_at - 1)
                self.assertEqual(len(deleted), fail_at - 1)
                self.assertTrue(fixture.manifest.is_file(), "manifest must be deleted last")
                after = snapshot(fixture.batch)
                if fail_at == 1:
                    self.assertEqual(after, before)
                for relative, value in after.items():
                    if relative != "." and stat.S_ISREG(value[1]):
                        self.assertEqual(value, before[relative])
                self.assertEqual(self.restore(fixture).returncode, 1)
                self.assertEqual(self.apply_purge(fixture)[0].returncode, 1)
                self.assertEqual(snapshot(fixture.batch), after)
                self.assertEqual(snapshot(neighbor.batch), neighbor_before)
                self.assertEqual(self.origins_snapshot(fixture), origin_before)
                self.assert_origins_empty(fixture)

    def test_new_uninventoried_file_during_deletion_is_preserved_and_stops(self):
        fixture = self.make_batch()
        neighbor = self.make_batch(1)
        neighbor_before = snapshot(neighbor.batch)
        real_unlink, inserted = os.unlink, []
        def race(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if not str(path).endswith(".tmp") and not inserted:
                intruder = fixture.payload / "UNINVENTORIED"
                intruder.write_bytes(b"MUST_SURVIVE")
                inserted.append(intruder)
            return result
        with mock.patch.object(os, "unlink", side_effect=race):
            result, _ = self.apply_purge(fixture)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0].read_bytes(), b"MUST_SURVIVE")
        self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "partial")
        self.assertTrue(fixture.manifest.exists())
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)

    def test_failed_partial_journal_update_retains_real_started_marker(self):
        fixture = self.make_batch()
        before = snapshot(fixture.batch)
        real_unlink = os.unlink
        def fail_payload(path, *args, **kwargs):
            if str(path).endswith(".tmp"):
                return real_unlink(path, *args, **kwargs)
            self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
            raise OSError(errno.EIO, "injected first payload failure")
        with mock.patch.object(os, "unlink", side_effect=fail_payload), \
                mock.patch.object(self.engine.Journal, "update",
                                  side_effect=self.engine.PurgeError("injected journal update failure")):
            result, _ = self.apply_purge(fixture)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
        self.assertEqual(snapshot(fixture.batch), before)
        self.assertEqual(self.restore(fixture).returncode, 1)
        self.assertEqual(self.apply_purge(fixture)[0].returncode, 1)
        self.assertEqual(snapshot(fixture.batch), before)

    def test_interrupt_at_complete_update_keeps_started_journal_without_false_assurance(self):
        fixture = self.make_batch()
        neighbor = self.make_batch(1)
        neighbor_before = snapshot(neighbor.batch)
        origin_before = self.origins_snapshot(fixture)
        updates, real_update = [], self.engine.Journal.update
        def interrupt_complete(journal, state, *args, **kwargs):
            updates.append(state)
            if state == "complete":
                self.assertFalse(fixture.batch.exists(), "deletion must really finish before interruption")
                self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
                raise KeyboardInterrupt
            return real_update(journal, state, *args, **kwargs)
        with mock.patch.object(self.engine.Journal, "update", autospec=True,
                               side_effect=interrupt_complete):
            result, _ = self.apply_purge(fixture)
        self.assertEqual(updates, ["complete"])
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertFalse(fixture.batch.exists())
        self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
        self.assertNotIn("nothing was deleted", (result.stdout + result.stderr).lower())
        self.assertIn("part or all", result.stderr)
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)
        self.assertEqual(self.origins_snapshot(fixture), origin_before)
        self.assert_origins_empty(fixture)
        after = snapshot(self.home)
        self.assertEqual(self.restore(fixture).returncode, 1)
        retry, confirm = self.apply_purge(fixture)
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        confirm.assert_not_called()
        self.assertEqual(snapshot(self.home), after)

    def test_unlink_then_interrupt_reports_uncertain_removal_and_blocks_restore_retry(self):
        fixture = self.make_batch()
        neighbor = self.make_batch(1)
        before, neighbor_before = snapshot(fixture.batch), snapshot(neighbor.batch)
        origin_before = self.origins_snapshot(fixture)
        files = {(p.stat().st_dev, p.stat().st_ino): p
                 for p in fixture.batch.rglob("*") if p.is_file()}
        removed, real_unlink = [], os.unlink
        def unlink_then_interrupt(path, *args, **kwargs):
            if str(path).endswith(".tmp"):
                return real_unlink(path, *args, **kwargs)
            st = os.stat(path, dir_fd=kwargs["dir_fd"], follow_symlinks=False)
            target = files[(st.st_dev, st.st_ino)]  # Only this selected synthetic batch.
            self.assertEqual(json.loads(self.journal(fixture).read_bytes())["state"], "started")
            real_unlink(path, *args, **kwargs)
            removed.append(str(target.relative_to(fixture.batch)))
            self.assertFalse(target.exists())
            raise KeyboardInterrupt
        with mock.patch.object(os, "unlink", side_effect=unlink_then_interrupt):
            result, _ = self.apply_purge(fixture)
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertEqual(len(removed), 1)
        self.assertNotIn("nothing was deleted", (result.stdout + result.stderr).lower())
        self.assertIn("plus possibly " + removed[0], result.stdout)
        journal = json.loads(self.journal(fixture).read_bytes())
        self.assertEqual(journal["state"], "partial")
        self.assertEqual(journal["deleted"], {"files": 0, "folders": 0})
        self.assertEqual(journal["stopped_at"], removed[0])
        self.assertIn("removal may or may not have happened", journal["error"])
        after = snapshot(fixture.batch)
        self.assertEqual(set(before) - set(after), set(removed))
        for relative, value in after.items():
            if stat.S_ISREG(value[1]):
                self.assertEqual(value, before[relative])
        self.assertTrue(fixture.manifest.is_file())
        self.assertEqual(self.restore(fixture).returncode, 1)
        self.assertEqual(self.apply_purge(fixture)[0].returncode, 1)
        self.assertEqual(snapshot(fixture.batch), after)
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)
        self.assertEqual(self.origins_snapshot(fixture), origin_before)
        self.assert_origins_empty(fixture)

    def _assert_pending_symlink_swap_refused(self, directory):
        neighbor = self.make_batch(1)
        previous = set(self.quarantine.glob("*/manifest.json"))
        if self.kind == "sessions":
            # Sorted scaffold: unlink .workspace-fork.lock first, then visit
            # checkpoints/ (nonempty), and workspace.yaml later in this parent.
            sid, source = self.scaffold(1)
            self.registry.write_text(self.row(sid))
            _, candidates = self.preview()
            self.ok(self.apply(candidates))
            container, first_name = "sessions", ".workspace-fork.lock"
            pending_name = "checkpoints" if directory else "workspace.yaml"
            origins = [source]
        else:
            # No publication: the first payload unlink is the run's .lock.
            # raw/ still holds private data; run.json sorts after it.
            rec = self.create(0, mode="normal")
            sid, source = rec["run_id"], self.path(rec["run_id"])
            _, lines = self.preview()
            self.assertEqual(len(lines), 1)
            self.ok(self.apply_lines(lines))
            container, first_name = "runs", ".lock"
            pending_name = "raw" if directory else "run.json"
            origins = [source, self.target(rec)]
        manifest, = set(self.quarantine.glob("*/manifest.json")) - previous
        fixture = types.SimpleNamespace(
            batch=manifest.parent, manifest=manifest,
            payload=manifest.parent / container / sid, origins=origins, id=sid)
        self.assert_origins_empty(fixture)
        # Also preserve a real synthetic live source, not just empty origins.
        if self.kind == "sessions":
            live_id, _ = self.session(3, fields={"user_named": "true"})
            self.registry.write_text(self.row(live_id, name="Kept live"))
        else:
            self.create(2, mode="normal")
        neighbor_before = snapshot(neighbor.batch)
        live_before = self.origins_snapshot(fixture)
        first = fixture.payload / first_name
        pending = fixture.payload / pending_name
        pending_rel = str(pending.relative_to(fixture.batch))
        first_rel = str(first.relative_to(fixture.batch))
        if directory:
            self.assertTrue(pending.is_dir())
            self.assertTrue(list(pending.iterdir()), "pending directory must still contain data")
        else:
            self.assertTrue(pending.is_file())
        pending_before = snapshot(pending)
        outside = self.home / "outside-selected-batch"
        outside.mkdir(mode=0o700)
        sentinel, parked = outside / "sentinel", outside / "saved-inventoried-object"
        if directory:
            sentinel.mkdir(mode=0o700)
            (sentinel / "never-touch.txt").write_bytes(b"OUTSIDE_DIRECTORY_SENTINEL")
        else:
            sentinel.write_bytes(b"OUTSIDE_FILE_SENTINEL")
        sentinel_before = snapshot(sentinel)
        digest = self.digest(fixture)
        files = {(p.stat().st_dev, p.stat().st_ino): str(p.relative_to(fixture.batch))
                 for p in fixture.batch.rglob("*") if p.is_file()}
        removed, real_unlink = [], os.unlink
        def swap_after_first_unlink(path, *args, **kwargs):
            if str(path).endswith(".tmp"):
                return real_unlink(path, *args, **kwargs)
            st = os.stat(path, dir_fd=kwargs["dir_fd"], follow_symlinks=False)
            self.assertIn((st.st_dev, st.st_ino), files,
                          "must refuse replacements, never unlink outside/uninventoried targets")
            relative = files[(st.st_dev, st.st_ino)]
            real_unlink(path, *args, **kwargs)
            removed.append(relative)
            if len(removed) == 1:
                self.assertEqual(relative, first_rel)
                self.assertFalse(first.exists(), "first inventoried payload really was unlinked")
                self.assertEqual(builds.call_count, 2, "both pre-delete plans must already have passed")
                journal = json.loads(self.journal(fixture).read_bytes())
                self.assertEqual((journal["state"], journal["snapshot"]), ("started", digest))
                self.assertEqual(snapshot(pending), pending_before, "replacement must still be pending")
                pending.rename(parked)
                pending.symlink_to(sentinel, target_is_directory=directory)
        with mock.patch.object(self.engine, "build_plan", wraps=self.engine.build_plan) as builds, \
                mock.patch.object(os, "unlink", side_effect=swap_after_first_unlink):
            result, confirmation = self.apply_purge(fixture, digest=digest)
        confirmation.assert_called_once()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PURGE STOPPED", result.stdout)
        self.assertTrue(removed)
        self.assertEqual(removed[0], first_rel)
        self.assertNotIn(pending_rel, removed)
        reason = pending_rel + ": changed since the confirmed snapshot"
        journal = json.loads(self.journal(fixture).read_bytes())
        self.assertEqual(journal["state"], "partial")
        self.assertEqual(journal["stopped_at"], pending_rel)
        self.assertEqual(journal["error"], reason)  # Exact identity check, not an ENOTEMPTY fallback.
        self.assertEqual(journal["deleted"]["files"], len(removed))
        self.assertIn("PURGE STOPPED part-way: " + reason, result.stdout)
        self.assertTrue(pending.is_symlink())
        self.assertEqual(os.readlink(pending), str(sentinel))
        self.assertTrue(fixture.manifest.is_file())
        after = snapshot(fixture.batch)
        with contextlib.ExitStack() as stack:
            restore_confirm = (stack.enter_context(mock.patch.object(self.helper, "confirm"))
                               if self.kind == "sessions" else None)
            restored = self.command("restore", "--batch", fixture.batch.name)
            if restore_confirm is not None:
                restore_confirm.assert_not_called()
        self.assertEqual(restored.returncode, 1, restored.stdout + restored.stderr)
        self.assertIn("permanent purge", (restored.stdout + restored.stderr).lower())
        retry, confirmation = self.apply_purge(fixture)
        self.assertEqual(retry.returncode, 1, retry.stdout + retry.stderr)
        confirmation.assert_not_called()
        self.assertEqual(snapshot(fixture.batch), after)
        self.assertEqual(snapshot(sentinel), sentinel_before)
        self.assertEqual(snapshot(parked), pending_before)
        self.assertEqual(snapshot(neighbor.batch), neighbor_before)
        self.assertEqual(self.origins_snapshot(fixture), live_before)
        for origin in fixture.origins:
            self.assertFalse(origin.exists())
        self.assertEqual(json.loads(self.journal(fixture).read_bytes()), journal)

    def test_pending_file_symlink_swap_after_payload_unlink_is_refused(self):
        self._assert_pending_symlink_swap_refused(directory=False)

    def test_pending_directory_symlink_swap_after_payload_unlink_is_refused(self):
        self._assert_pending_symlink_swap_refused(directory=True)


class TestSessionPurge(PurgeContracts, sessions.CleanupHomeTestCase):
    kind = "sessions"


class TestReportPurge(PurgeContracts, reports.CleanupHome):
    kind = "reports"

    def test_purge_never_acquires_live_report_or_publication_locks(self):
        fixture = self.make_batch()
        before = self.origins_snapshot(fixture)
        real_acquire = self.helper.acquire_global_lock
        names = []
        def acquire(fd, name, *args):
            names.append(name)
            self.assertEqual(name, ".transaction.lock")
            return real_acquire(fd, name, *args)
        with mock.patch.object(self.helper, "acquire_global_lock", side_effect=acquire), \
                mock.patch.object(self.helper, "acquire_run_lock",
                                  side_effect=AssertionError("must not lock live runs")):
            result, _ = self.apply_purge(fixture)
        self.ok(result)
        self.assertEqual(names, [".transaction.lock"])
        self.assertEqual(self.origins_snapshot(fixture), before)


class TestTerminalProtocol(unittest.TestCase):
    def test_real_terminal_flush_exact_phrase_and_short_timeout(self):
        engine = load_engine()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        # A complete correct line was already typed: tcflush must discard it.
        os.write(master, b"PURGE sessions fixture 123456789abc\n")
        self.assertIsNone(engine.confirm_on_tty(slave, "PURGE sessions fixture 123456789abc", .01))
        with mock.patch.object(engine.select, "select", return_value=([slave], [], [])), \
                mock.patch.object(engine.os, "read", return_value=b" PURGE sessions fixture 123456789abc\n"):
            self.assertFalse(engine.confirm_on_tty(slave, "PURGE sessions fixture 123456789abc", 1))

    def test_fixed_limits_and_preview_print_cap(self):
        engine = load_engine()
        self.assertEqual((engine.MAX_ENTRIES, engine.MAX_DEPTH, engine.MAX_ITEM_LINES), (20000, 24, 100))
        st = Path(os.environ["HOME"]).stat()  # Runner-owned scratch HOME only.
        inventory = engine.Inventory()
        inventory.add(engine.Node((), "d", st, []))
        verification = engine.Verification(
            (st.st_dev, st.st_ino), {}, (), [([()], "synthetic-%03d" % i) for i in range(105)], {}, 105)
        plan = engine.Plan("synthetic", os.environ["HOME"], (st.st_dev, st.st_ino),
                           inventory, verification, "a" * 64)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            engine.print_plan(types.SimpleNamespace(title="synthetic"), plan)
        self.assertEqual(output.getvalue().count("    - synthetic-"), 100)
        self.assertIn("and 5 more", output.getvalue())
        self.assertNotIn("synthetic-100", output.getvalue())

    def test_owner_device_flags_and_inaccessible_directory_checks(self):
        engine = load_engine()
        base = dict(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(),
                    st_dev=42, st_nlink=1, st_blocks=8, st_flags=0)
        cases = [
            ({"st_uid": os.getuid() + 1}, "not owned"),
            ({"st_dev": 43}, "another filesystem"),
            ({"st_mode": stat.S_IFDIR | 0o500}, "not fully accessible"),
            ({"st_flags": engine.BLOCKING_FLAGS}, "immutable"),
        ]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(engine.Refused, reason):
                    engine._check_entry(types.SimpleNamespace(**{**base, **changes}),
                                        ("synthetic",), os.getuid(), 42)


class TestPurgeMenus(menus.BaseHomeTestCase):
    menu_env = reports.TestReportCleanupMenu.menu_env
    calls = reports.TestReportCleanupMenu.calls
    clean_log = reports.TestReportCleanupMenu.clean_log
    no_launch = reports.TestReportCleanupMenu.no_launch
    _session = reports.TestReportCleanupMenu._session
    _scripted_tty = reports.TestReportCleanupMenu._scripted_tty
    EMPTY = reports.TestReportCleanupMenu.EMPTY
    SELECT = reports.TestReportCleanupMenu.SELECT
    SUB = reports.TestReportCleanupMenu.SUB
    PICK = "Batch to purge PERMANENTLY (number; Enter cancels):"

    def setUp(self):
        super().setUp()
        self.cwd = os.path.join(self.home, "worktree")
        self.state_dir = os.path.join(self.home, ".copilot/session-state")
        self.fake_bin = os.path.join(self.home, "fake-bin")
        for path in (self.cwd, self.state_dir, self.fake_bin):
            os.makedirs(path, exist_ok=True)
        self.copilot_log = os.path.join(self.home, "forbidden-copilot-call")
        reports.report_fixture.write(
            Path(self.fake_bin) / "copilot",
            "#!/bin/sh\nprintf called >> \"$COPILOT_LOG\"\nexit 95\n", True)
        (Path(self.fake_bin) / "python3").symlink_to(sys.executable)
        self.log = Path(self.home) / "cleanup-calls.jsonl"
        self.helper = Path(self.home) / "fake-purge.py"
        self.task = Path(self.home) / "fake-task.py"
        self.task.write_text("import sys\n")

    def purge_env(self, extra=None):
        self.helper.write_text(
            "import json,os,sys\n"
            "a=sys.argv[1:]\n"
            "with open(os.environ['CLEANUP_CALL_LOG'],'a') as f: "
            "f.write(json.dumps({'args':a})+'\\n')\n"
            "cmd=next(s for s in a if s in ('purge-list','purge'))\n"
            "if os.environ.get('FAIL_COMMAND')==cmd:\n"
            " print('injected purge failure');sys.exit(9)\n"
            "if cmd=='purge-list': print('owned batch\\nPURGEABLE\\t%s')\n"
            "else: print('Purged fixture permanently' if os.environ.get('PURGE_OUTCOME')=='purged' "
            "else 'Cancelled. Nothing was deleted.')\n" % reports.BATCH)
        return self.menu_env({"COPILOT_SESSION_CLEANUP_HELPER": str(self.helper), **(extra or {})})

    def test_x_and_report_4_both_menu_variants_cancel_or_failure_never_launch(self):
        for populated in (False, True):
            if populated:
                self._session("kept", name="Saved fixture")
            for kind in ("sessions", "reports"):
                for outcome in ("purged", "cancel", "purge-list", "purge"):
                    failure = outcome if outcome in ("purge-list", "purge") else ""
                    with self.subTest(populated=populated, kind=kind, outcome=outcome):
                        self.clean_log()
                        prompt = self.SELECT if populated else self.EMPTY
                        actions = [(prompt, "x\n" if kind == "sessions" else "r\n")]
                        if kind == "reports":
                            actions += [(self.SUB, "4\n")]
                        if failure != "purge-list":
                            actions += [(self.PICK, "1\n")]
                        if kind == "reports":
                            actions += [(self.SUB, "b\n")]
                        actions += [(prompt, "q\n")]
                        status, out = self._scripted_tty(
                            self.purge_env({"FAIL_COMMAND": failure, "PURGE_OUTCOME": outcome}), actions)
                        self.no_launch(status, out)
                        self.assertNotIn("PURGEABLE\t", out)
                        calls = [call["args"] for call in self.calls()]
                        self.assertEqual(len(calls), 1 if failure == "purge-list" else 2)
                        self.assertIn("purge-list", calls[0])
                        if len(calls) == 2:
                            self.assertEqual(calls[1][-3:], ["purge", "--batch", reports.BATCH])
                        if kind == "sessions":
                            self.assertEqual(calls[0][:6], [
                                "--session-state-dir", self.state_dir, "--sessions-file",
                                os.path.join(self.home, ".copilot-sessions"), "--quarantine-dir",
                                os.path.join(self.home, ".copilot-s-quarantine")])
                        if failure:
                            self.assertIn("exit 9", out)
                        else:
                            self.assertIn("Purged fixture" if outcome == "purged" else "Cancelled.", out)

    def test_picker_back_and_invalid_selection_never_call_purge(self):
        for key in ("x", "r"):
            for pick in ("\n", "0\n", "2\n", "yes\n", "\x04"):
                with self.subTest(key=key, pick=repr(pick)):
                    self.clean_log()
                    actions = [(self.EMPTY, key + "\n")]
                    if key == "r":
                        actions += [(self.SUB, "4\n")]
                    actions += [(self.PICK, pick)]
                    if key == "r":
                        actions += [(self.SUB, "\n")]
                    actions += [(self.EMPTY, "q\n")]
                    status, out = self._scripted_tty(self.purge_env(), actions)
                    self.no_launch(status, out)
                    self.assertEqual(len(self.calls()), 1)
                    self.assertIn("purge-list", self.calls()[0]["args"])


class TestPurgeDistribution(menus.BaseHomeTestCase):
    def test_copy_and_symlink_ship_engine_and_uninstall_preserves_both_journals(self):
        home = Path(self.home)
        roots = [home / ".copilot-s-quarantine", home / ".copilot/copilot-s-report-quarantine"]
        for root in roots:
            marker = root / ".purge" / (reports.BATCH + ".json")
            marker.parent.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            marker.write_bytes(b"SYNTHETIC_PURGE_JOURNAL")
            payload = root / reports.BATCH / "synthetic-survivor"
            payload.parent.mkdir(mode=0o700)
            payload.write_bytes(b"SYNTHETIC_PARTIAL_SURVIVOR")
        before = [snapshot(root) for root in roots]
        for copy_mode in (False, True):
            with self.subTest(copy=copy_mode):
                prefix = home / ("copy-bin" if copy_mode else "symlink-bin")
                result = menus.run([menus.INSTALL_SH, "--prefix", str(prefix),
                                    *(["--copy"] if copy_mode else [])], env=self.env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                engine = prefix / ENGINE.name
                self.assertEqual(engine.is_symlink(), not copy_mode)
                self.assertEqual(engine.read_bytes(), ENGINE.read_bytes())
                manifest = home / ".copilot-cli-toolkit/install-manifest.txt"
                self.assertIn(str(engine), manifest.read_text().splitlines())
                for helper in ("copilot-session-cleanup.py", "copilot-report-cleanup.py"):
                    result = menus.run([sys.executable, str(prefix / helper), "purge-list"],
                                       env=self.env())
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("unreadable", result.stdout)
                result = menus.run([menus.UNINSTALL_SH], env=self.env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(os.path.lexists(engine))
                self.assertEqual([snapshot(root) for root in roots], before)


if __name__ == "__main__":
    unittest.main()
