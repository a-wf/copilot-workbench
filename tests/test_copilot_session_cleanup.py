#!/usr/bin/env python3
"""Recoverable cleanup contracts, stdlib-only and confined to scratch HOME.

Fresh ctime cannot be backdated on macOS/Linux. Positive fixtures use old
metadata/mtime and patch only the helper clock two days forward; actual stat,
fingerprint, lsof subprocess, rename, registry and manifest operations remain
real. The unmodified helper CLI separately verifies the fresh-ctime veto.
"""

import contextlib
import datetime
import errno
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HELPER_PATH = Path(__file__).resolve().parents[1] / "bin/copilot-session-cleanup.py"
SPEC = importlib.util.spec_from_file_location("session_cleanup", HELPER_PATH)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def session_id(number):
    return "12345678-1234-1234-abcd-%012x" % number


class CleanupHomeTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="copilot-cleanup-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / ".copilot/session-state"
        self.state.mkdir(parents=True)
        self.registry = self.home / ".copilot-sessions"
        self.quarantine = self.home / ".copilot-s-quarantine"
        self.cwd = self.home / "worktree"
        self.cwd.mkdir()
        self.fake_bin = self.home / "fake-bin"
        self.fake_bin.mkdir()
        self.lsof = self.fake_bin / "lsof"
        self.lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.lsof.chmod(0o755)
        (self.fake_bin / "copilot").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (self.fake_bin / "copilot").chmod(0o755)
        self.environment = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(
                [str(self.fake_bin), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
            ),
            "COPILOT_TASK_REPORT_PRICING_FETCH": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env_patch = mock.patch.dict(os.environ, self.environment, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        actual_now = time.time()
        self.now = actual_now + 2 * cleanup.MIN_AGE_SECONDS
        self.old_epoch = actual_now - 2 * cleanup.MIN_AGE_SECONDS
        self.old = datetime.datetime.fromtimestamp(
            self.old_epoch, datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.clock = mock.patch.object(cleanup, "now_epoch", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def session(self, number, fields=None, raw=None, cwd=None):
        sid = session_id(number)
        directory = self.state / sid
        directory.mkdir()
        metadata = {
            "id": sid, "cwd": str(cwd or self.cwd), "user_named": "false",
            "created_at": self.old, "updated_at": self.old,
            "summary_count": "0", "fork_count": "0", "host_type": "local",
        }
        for key, value in (fields or {}).items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = value
        text = raw if raw is not None else "".join(
            "%s: %s\n" % item for item in metadata.items()
        )
        (directory / "workspace.yaml").write_text(text, encoding="utf-8")
        os.utime(directory / "workspace.yaml", (self.old_epoch, self.old_epoch))
        os.utime(directory, (self.old_epoch, self.old_epoch))
        return sid, directory

    def args(self, *arguments):
        return [
            "--session-state-dir", str(self.state),
            "--sessions-file", str(self.registry),
            "--quarantine-dir", str(self.quarantine), *arguments,
        ]

    def invoke(self, *arguments, data=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(data)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cleanup.main(self.args(*arguments))
        return types.SimpleNamespace(
            returncode=status, stdout=stdout.getvalue(), stderr=stderr.getvalue()
        )

    def preview(self, all_directories=False):
        scope = ["--all"] if all_directories else ["--cwd", str(self.cwd)]
        result = self.invoke("preview", *scope)
        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = {}
        for line in result.stdout.splitlines():
            if line.startswith("CANDIDATE\t"):
                _, sid, fingerprint = line.split("\t")
                candidates[sid] = fingerprint
        return result, candidates

    def apply(self, candidates):
        return self.invoke(
            "apply", "--all",
            data="".join("%s\t%s\n" % item for item in candidates.items()),
        )

    def row(self, sid, name="unnamed"):
        return "%s|%s|%s|%s\n" % (sid, name, self.old, self.cwd)

    def manifest(self):
        paths = list(self.quarantine.glob("*/manifest.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    def moved_fixture(self):
        sid, source = self.session(1)
        self.registry.write_text(self.row(sid), encoding="utf-8")
        _, candidates = self.preview()
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest_path, manifest = self.manifest()
        destination = manifest_path.parent / "sessions" / sid
        return sid, source, destination, manifest_path, manifest


class TestEligibility(CleanupHomeTestCase):
    def test_preview_counts_exact_cwd_and_all_without_writes(self):
        local, _ = self.session(1)
        foreign, _ = self.session(2, cwd=self.home / "other")
        named, _ = self.session(3, {"user_named": "true"})
        self.registry.write_text(self.row(local), encoding="utf-8")
        before = self.registry.read_bytes()
        local_result, local_ids = self.preview()
        all_result, all_ids = self.preview(all_directories=True)
        self.assertEqual(set(local_ids), {local})
        self.assertEqual(set(all_ids), {local, foreign})
        self.assertNotIn(named, all_ids)
        self.assertIn("1 empty session(s)", local_result.stdout)
        self.assertIn("2 empty session(s)", all_result.stdout)
        self.assertIn("Kept (not eligible): 1", local_result.stdout)
        self.assertIn("~/other", all_result.stdout)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse(self.quarantine.exists())

    def test_metadata_requires_explicit_unnamed_consistent_plain_scalars(self):
        rejected = [
            {"user_named": None}, {"user_named": "False"}, {"user_named": "true"},
            {"id": session_id(999)}, {"cwd": "relative"}, {"cwd": str(self.cwd) + "/../x"},
            {"updated_at": "not-a-date"}, {"created_at": "2026-02-30T00:00:00Z"},
            {"name": ""}, {"summary": ""}, {"summary_count": "1"}, {"fork_count": "1"},
            {"origin": "cloud"}, {"branch": "'quoted'"},
        ]
        for number, fields in enumerate(rejected, 1):
            with self.subTest(fields=fields):
                sid, _ = self.session(number, fields)
                _, candidates = self.preview(all_directories=True)
                self.assertNotIn(sid, candidates)
        self.session(100, raw="id: one\nid: two\ncwd: /tmp\nuser_named: false\n")
        self.assertEqual(self.preview(all_directories=True)[1], {})

    def test_timestamp_skew_accepts_one_second_but_not_more_or_recent_metadata(self):
        base = cleanup.parse_timestamp(self.old)
        stamp = lambda epoch: datetime.datetime.fromtimestamp(
            epoch, datetime.timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        valid, _ = self.session(1, {"updated_at": stamp(base + 1)})
        used, _ = self.session(2, {"updated_at": stamp(base + 1.001)})
        mismatch, _ = self.session(3, {"updated_at": stamp(base - 1.001)})
        recent, _ = self.session(4, {
            "created_at": stamp(self.now - 60), "updated_at": stamp(self.now - 60),
        })
        _, candidates = self.preview()
        self.assertEqual(set(candidates), {valid})
        self.assertTrue({used, mismatch, recent}.isdisjoint(candidates))

    def test_filesystem_age_and_real_cli_fresh_ctime_fail_closed(self):
        sid, directory = self.session(1)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), *self.args("preview", "--all")],
            env=self.environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CANDIDATE\t", result.stdout)
        self.assertIn("within the last 24h", result.stdout)
        for path in (directory, directory / "workspace.yaml"):
            with self.subTest(path=path):
                os.utime(path, (self.now - 60, self.now - 60))
                self.assertNotIn(sid, self.preview()[1])
                os.utime(path, (self.old_epoch, self.old_epoch))

    def test_any_history_artifact_missing_workspace_or_unsafe_link_excludes(self):
        for number, artifact in enumerate(
            ["events.jsonl", "notes.md", "copilot-s-context.json", "subdirectory"], 1
        ):
            sid, directory = self.session(number)
            if artifact == "subdirectory":
                (directory / artifact).mkdir()
            else:
                (directory / artifact).write_bytes(b"")
            self.assertNotIn(sid, self.preview()[1])
        sid, directory = self.session(10)
        os.link(directory / "workspace.yaml", self.home / "workspace-hardlink")
        self.assertNotIn(sid, self.preview()[1])
        sid, directory = self.session(11)
        (directory / "workspace.yaml").unlink()
        (directory / "workspace.yaml").symlink_to(self.home / "workspace-hardlink")
        self.assertNotIn(sid, self.preview()[1])
        sid, directory = self.session(12)
        outside = self.home / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        self.assertNotIn(sid, self.preview(all_directories=True)[1])
        (self.state / session_id(13)).mkdir()
        (self.state / session_id(14).upper()).mkdir()
        self.assertEqual(self.preview(all_directories=True)[1], {})

    def test_custom_alias_current_environment_and_unreadable_registry_exclude(self):
        sid, _ = self.session(1)
        for name in ("unnamed", "", self.cwd.name + " session"):
            self.registry.write_text(self.row(sid, name), encoding="utf-8")
            self.assertIn(sid, self.preview()[1])
        self.registry.write_text(self.row(sid, "Deliberate alias"), encoding="utf-8")
        self.assertNotIn(sid, self.preview()[1])
        self.registry.unlink()
        with mock.patch.dict(os.environ, {"ACTIVE_CONTEXT": "prefix-" + sid.upper()}):
            self.assertNotIn(sid, self.preview()[1])
        target = self.home / "index"
        target.write_text(self.row(sid), encoding="utf-8")
        self.registry.symlink_to(target)
        self.assertNotIn(sid, self.preview()[1])
        self.assertEqual(target.read_text(encoding="utf-8"), self.row(sid))

    def test_lsof_closed_open_error_missing_and_timeout(self):
        sid, _ = self.session(1)
        for script in (
            "#!/bin/sh\nprintf 'p123\\n'\nexit 0\n",
            "#!/bin/sh\nprintf 'error\\n' >&2\nexit 1\n",
            "#!/bin/sh\nexit 2\n",
            "#!/bin/sh\nexit 0\n",
        ):
            with self.subTest(script=script):
                self.lsof.write_text(script, encoding="utf-8")
                self.assertNotIn(sid, self.preview()[1])
        with mock.patch.object(cleanup, "find_lsof", return_value=None):
            self.assertNotIn(sid, self.preview()[1])
        with mock.patch.object(cleanup.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 15)):
            self.assertNotIn(sid, self.preview()[1])
        self.lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.assertIn(sid, self.preview()[1])

    def test_symlink_state_root_and_traversal_candidates_are_refused(self):
        outside = self.home / "outside-state"
        self.state.rename(outside)
        self.state.symlink_to(outside, target_is_directory=True)
        result = self.invoke("preview", "--all")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinks", result.stderr)
        for data in ("../outside\t" + "a" * 64 + "\n", session_id(1) + "\tbad\n"):
            with self.subTest(data=data):
                result = self.invoke("apply", "--all", data=data)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("malformed candidate", result.stderr)
        self.assertFalse(self.quarantine.exists())


class TestQuarantineAndRestore(CleanupHomeTestCase):
    def test_exact_rename_manifest_registry_and_restore_roundtrip(self):
        sid, source = self.session(1)
        failed_id, failed_source = self.session(2)
        original_stat = source.stat()
        workspace_bytes = (source / "workspace.yaml").read_bytes()
        native_index = self.state.parent / "session-store.db"
        native_index.write_bytes(b"native-index-must-not-change")
        unrelated = "unrelated|Custom alias|timestamp|/elsewhere\n\nmalformed row\n"
        self.registry.write_text(
            self.row(sid) + unrelated + self.row(failed_id), encoding="utf-8"
        )
        _, candidates = self.preview()
        rename = cleanup.rename_noreplace_at
        def move(src_fd, src_name, dst_fd, dst_name):
            if src_name == failed_id:
                raise OSError(errno.EXDEV, "cross-device")
            return rename(src_fd, src_name, dst_fd, dst_name)
        with mock.patch.object(cleanup, "rename_noreplace_at", side_effect=move), \
                mock.patch.object(cleanup.shutil, "copytree", side_effect=AssertionError("no copies")):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        path, manifest = self.manifest()
        destination = path.parent / "sessions" / sid
        self.assertFalse(source.exists())
        self.assertTrue(failed_source.exists())
        self.assertEqual(destination.stat().st_ino, original_stat.st_ino)
        self.assertEqual((destination / "workspace.yaml").read_bytes(), workspace_bytes)
        self.assertEqual(self.registry.read_text(encoding="utf-8"), unrelated + self.row(failed_id))
        entries = {entry["id"]: entry for entry in manifest["entries"]}
        self.assertEqual(entries[sid]["state"], "moved")
        self.assertEqual(entries[sid]["registry_rows"], [self.row(sid).rstrip("\n")])
        self.assertEqual(entries[failed_id]["state"], "failed")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.quarantine.stat().st_mode), 0o700)
        self.assertIn("moved 1", self.invoke("list").stdout)
        for answer in ("", "\n", "Yes\n", "y\n", "no\n"):
            result = self.invoke("restore", "--batch", manifest["batch"], data=answer)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Cancelled", result.stdout)
            self.assertFalse(source.exists())
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(source.stat().st_ino, original_stat.st_ino)
        self.assertFalse(destination.exists())
        self.assertIn(self.row(sid), self.registry.read_text(encoding="utf-8"))
        self.assertEqual(native_index.read_bytes(), b"native-index-must-not-change")
        self.assertTrue(path.exists(), "recoverable manifest is retained, never purged")

    def test_confirmation_requires_exact_yes_not_padded_yes(self):
        for answer in (" yes\n", "yes \n", "yes\t\n"):
            with self.subTest(answer=answer), mock.patch.object(sys, "stdin", io.StringIO(answer)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(cleanup.confirm("confirm: "))

    def test_freezes_ids_and_revalidates_metadata_alias_and_new_artifacts(self):
        first, first_dir = self.session(1)
        metadata, metadata_dir = self.session(2)
        alias, alias_dir = self.session(3)
        artifacts, artifacts_dir = self.session(4)
        self.registry.write_text(self.row(first) + self.row(alias), encoding="utf-8")
        _, candidates = self.preview()
        with (metadata_dir / "workspace.yaml").open("a", encoding="utf-8") as handle:
            handle.write("branch: changed-after-preview\n")
        self.registry.write_text(self.row(first) + self.row(alias, "New alias"), encoding="utf-8")
        (artifacts_dir / "events.jsonl").write_bytes(b"")
        late, late_dir = self.session(5)
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 1   Skipped: 3", result.stdout)
        self.assertFalse(first_dir.exists())
        for directory in (metadata_dir, alias_dir, artifacts_dir, late_dir):
            self.assertTrue(directory.exists())
        self.assertIn(self.row(alias, "New alias"), self.registry.read_text(encoding="utf-8"))
        self.assertNotIn(late, {entry["id"] for entry in self.manifest()[1]["entries"]})

    def test_apply_rechecks_activity_after_closed_preview_for_open_and_error(self):
        sid, source = self.session(1)
        original = (self.row(sid) + "unrelated|Keep alias|timestamp|/other\n").encode()
        self.registry.write_bytes(original)
        source_inode = source.stat().st_ino
        for script in (
            "#!/bin/sh\nprintf 'p123\\n'\nexit 0\n",
            "#!/bin/sh\nprintf 'fixture activity error\\n' >&2\nexit 2\n",
        ):
            with self.subTest(lsof=script):
                self.lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                _, candidates = self.preview()
                self.assertEqual(set(candidates), {sid})
                previous = set(self.quarantine.glob("*/manifest.json"))
                self.lsof.write_text(script, encoding="utf-8")
                result = self.apply(candidates)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Moved: 0   Skipped: 1", result.stdout)
                self.assertEqual(source.stat().st_ino, source_inode)
                self.assertEqual(self.registry.read_bytes(), original)
                created = set(self.quarantine.glob("*/manifest.json")) - previous
                self.assertEqual(len(created), 1)
                path = created.pop()
                entry = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
                self.assertEqual(entry["state"], "skipped")
                self.assertFalse((path.parent / "sessions" / sid).exists())

    def test_postmove_mutation_uses_real_reverse_rename_and_preserves_registry(self):
        sid, source = self.session(1)
        source_inode = source.stat().st_ino
        state_inode = self.state.stat().st_ino
        workspace = (source / "workspace.yaml").read_bytes()
        original = (self.row(sid) + "unrelated|Keep alias|timestamp|/other\n").encode()
        self.registry.write_bytes(original)
        _, candidates = self.preview()
        payload = b"activity created during the move\n"
        rename = cleanup.rename_noreplace_at
        moves = []
        def mutate_after_forward(src_fd, src_name, dst_fd, dst_name):
            source_parent = os.fstat(src_fd).st_ino
            destination_parent = os.fstat(dst_fd).st_ino
            result = rename(src_fd, src_name, dst_fd, dst_name)
            moves.append((source_parent, destination_parent))
            if source_parent == state_inode:
                directory_fd = os.open(
                    dst_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd
                )
                try:
                    fd = os.open(
                        "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=directory_fd,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(payload)
                finally:
                    os.close(directory_fd)
            return result
        with mock.patch.object(cleanup, "rename_noreplace_at", side_effect=mutate_after_forward):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 1", result.stdout)
        self.assertEqual(len(moves), 2, "forward and reverse must both perform real moves")
        self.assertEqual(moves[0], (state_inode, moves[1][0]))
        self.assertEqual(moves[1][1], state_inode)
        self.assertEqual(source.stat().st_ino, source_inode)
        self.assertEqual((source / "workspace.yaml").read_bytes(), workspace)
        self.assertEqual((source / "events.jsonl").read_bytes(), payload)
        self.assertEqual(self.registry.read_bytes(), original)
        path, manifest = self.manifest()
        self.assertEqual(manifest["entries"][0]["state"], "skipped")
        self.assertFalse((path.parent / "sessions" / sid).exists())

    def test_registry_write_failure_retains_quarantine_and_restore_without_duplicate_rows(self):
        sid, source = self.session(1)
        source_inode = source.stat().st_ino
        workspace = (source / "workspace.yaml").read_bytes()
        original = (self.row(sid) + "unrelated|Keep alias|timestamp|/other\n").encode()
        self.registry.write_bytes(original)
        _, candidates = self.preview()
        with mock.patch.object(
            cleanup, "write_registry_lines", side_effect=OSError("fixture registry write failure")
        ) as failing_write:
            result = self.apply(candidates)
        failing_write.assert_called_once()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Warning:", result.stdout)
        self.assertIn("fixture registry write failure", result.stdout)
        self.assertFalse(source.exists())
        self.assertEqual(self.registry.read_bytes(), original)
        path, manifest = self.manifest()
        destination = path.parent / "sessions" / sid
        self.assertEqual(destination.stat().st_ino, source_inode)
        self.assertEqual((destination / "workspace.yaml").read_bytes(), workspace)
        entry = manifest["entries"][0]
        self.assertEqual(entry["state"], "moved")
        self.assertEqual(entry["registry"], "update_failed")
        self.assertEqual(entry["registry_rows"], [self.row(sid).rstrip("\n")])
        restored = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(source.stat().st_ino, source_inode)
        self.assertEqual((source / "workspace.yaml").read_bytes(), workspace)
        self.assertFalse(destination.exists())
        self.assertEqual(self.registry.read_bytes(), original)
        rows = self.registry.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith(sid + "|") for line in rows), 1)

    def test_revalidates_registry_again_between_individual_moves(self):
        first, first_dir = self.session(1)
        second, second_dir = self.session(2)
        self.registry.write_text(self.row(first) + self.row(second), encoding="utf-8")
        _, candidates = self.preview()
        quarantine_one = cleanup.quarantine_one
        def move(ctx, scope, entry, batch_fd, sessions_fd, manifest):
            result = quarantine_one(ctx, scope, entry, batch_fd, sessions_fd, manifest)
            if entry["id"] == first:
                # Add the alias cooperatively after the first candidate's
                # complete move/removal transaction has released its lock.
                with cleanup.RegistryLock(str(self.registry)):
                    lines = cleanup.read_registry_lines(str(self.registry))
                    lines = [
                        self.row(second, "Named during cleanup").rstrip("\n")
                        if cleanup.row_id(line) == second else line for line in lines
                    ]
                    cleanup.write_registry_lines(str(self.registry), lines)
            return result
        with mock.patch.object(cleanup, "quarantine_one", side_effect=move):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 1   Skipped: 1", result.stdout)
        self.assertFalse(first_dir.exists())
        self.assertTrue(second_dir.exists(), result.stdout)
        self.assertIn(self.row(second, "Named during cleanup"), self.registry.read_text(encoding="utf-8"))

    def test_restore_collision_changed_contents_and_symlink_entry_do_not_overwrite(self):
        sid, source, destination, _, manifest = self.moved_fixture()
        source.mkdir()
        (source / "keep.txt").write_bytes(b"collision")
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((source / "keep.txt").read_bytes(), b"collision")
        shutil.rmtree(source)
        workspace = destination / "workspace.yaml"
        original = workspace.read_bytes()
        workspace.write_bytes(original + b"branch: edited\n")
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("contents changed", result.stdout)
        self.assertFalse(source.exists())
        external = self.home / "external"
        destination.rename(external)
        destination.symlink_to(external, target_is_directory=True)
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(destination.is_symlink())
        self.assertTrue(external.exists())
        self.assertFalse(source.exists())

    def test_restore_checks_collision_again_after_confirmation(self):
        _, source, destination, _, manifest = self.moved_fixture()
        def confirm(_):
            source.mkdir()
            return True
        with mock.patch.object(cleanup, "confirm", side_effect=confirm):
            result = self.invoke("restore", "--batch", manifest["batch"])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(destination.exists())
        self.assertEqual(list(source.iterdir()), [])

    def test_restore_does_not_replace_collision_created_during_manifest_write(self):
        _, source, destination, _, manifest = self.moved_fixture()
        save = cleanup.save_manifest
        collision_inode = []
        def save_with_collision(batch_dir, data):
            result = save(batch_dir, data)
            if any(entry["state"] == "restoring" for entry in data["entries"]):
                source.mkdir()
                collision_inode.append(source.stat().st_ino)
            return result
        with mock.patch.object(cleanup, "save_manifest", side_effect=save_with_collision):
            result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertTrue(destination.exists(), result.stdout)
        self.assertEqual(source.stat().st_ino, collision_inode[0], result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_restore_refuses_symlink_quarantine_sessions_parent(self):
        _, source, destination, path, manifest = self.moved_fixture()
        sessions = path.parent / "sessions"
        outside = self.home / "outside-quarantine"
        sessions.rename(outside)
        sessions.symlink_to(outside, target_is_directory=True)
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(source.exists())
        self.assertTrue((outside / destination.name).exists())

    def test_quarantine_root_symlink_and_initial_manifest_failure_move_nothing(self):
        sid, source = self.session(1)
        _, candidates = self.preview()
        outside = self.home / "outside"
        outside.mkdir()
        self.quarantine.symlink_to(outside, target_is_directory=True)
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(source.exists())
        self.assertEqual(list(outside.iterdir()), [])
        self.quarantine.unlink()
        with mock.patch.object(cleanup, "save_manifest", side_effect=OSError("disk full")):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing was moved", result.stderr)
        self.assertTrue(source.exists())

    def test_final_manifest_write_failure_still_recovers_removed_registry_rows(self):
        sid, source = self.session(1)
        original_row = self.row(sid)
        self.registry.write_text(original_row, encoding="utf-8")
        _, candidates = self.preview()
        save = cleanup.save_manifest
        def fail_final(batch_dir, manifest):
            if any(entry.get("registry") == "removed" for entry in manifest["entries"]):
                raise OSError("simulated final manifest disk failure")
            return save(batch_dir, manifest)
        with mock.patch.object(cleanup, "save_manifest", side_effect=fail_final):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(source.exists())
        _, manifest = self.manifest()
        entry = manifest["entries"][0]
        self.assertEqual(entry["registry_rows"], [original_row.rstrip("\n")])
        self.assertEqual(entry["registry"], "removing")
        restored = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertTrue(source.exists())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), original_row)

    def test_restore_preserves_existing_registry_alias_and_reconciles_pending_move(self):
        sid, source, destination, path, manifest = self.moved_fixture()
        existing = self.row(sid, "Alias created while quarantined")
        self.registry.write_text(existing, encoding="utf-8")
        manifest["entries"][0]["state"] = "pending"
        cleanup.save_manifest(str(path.parent), manifest)
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(destination.exists())
        self.assertTrue(source.exists())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), existing)
        manifest["entries"][0]["state"] = "pending"
        cleanup.save_manifest(str(path.parent), manifest)
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(path.read_text())["entries"][0]["state"], "skipped")

    def test_duplicate_candidates_and_empty_apply_never_create_quarantine(self):
        sid, _ = self.session(1)
        _, candidates = self.preview()
        data = "%s\t%s\n" % (sid, candidates[sid])
        result = self.invoke("apply", "--all", data=data + data)
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate candidate", result.stderr)
        result = self.apply({})
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.quarantine.exists())

    def test_held_flock_skips_candidate_without_registry_or_session_mutation(self):
        sid, source = self.session(1)
        original = self.row(sid)
        self.registry.write_text(original, encoding="utf-8")
        _, candidates = self.preview()
        lock_path = Path(str(self.registry) + ".lock")
        with lock_path.open("a") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = lock_path.stat().st_ino
            with mock.patch.object(cleanup, "LOCK_TIMEOUT_SECONDS", 0.05):
                result = self.apply(candidates)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Moved: 0   Skipped: 1", result.stdout)
            self.assertIn("held by another process", result.stdout)
            self.assertTrue(source.exists())
            self.assertEqual(self.registry.read_text(encoding="utf-8"), original)
            self.assertEqual(lock_path.stat().st_ino, inode)
            with lock_path.open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class TestAtomicMoves(CleanupHomeTestCase):
    def test_real_noreplace_syscall_preserves_existing_targets_and_directory_inode(self):
        sid, source = self.session(1)
        original_inode = source.stat().st_ino
        contents = (source / "workspace.yaml").read_bytes()
        targets = self.home / "rename-targets"
        targets.mkdir()
        external = self.home / "external-sentinel"
        external.write_bytes(b"do not overwrite")
        (targets / "empty-directory").mkdir()
        (targets / "regular-file").write_bytes(b"existing target")
        (targets / "symlink").symlink_to(external)
        source_fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        target_fd = os.open(targets, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in ("empty-directory", "regular-file", "symlink"):
                with self.subTest(target=name):
                    before = (targets / name).lstat()
                    with self.assertRaises(OSError) as failure:
                        cleanup.rename_noreplace_at(source_fd, sid, target_fd, name)
                    self.assertIn(failure.exception.errno, (errno.EEXIST, errno.ENOTEMPTY))
                    self.assertEqual((targets / name).lstat().st_ino, before.st_ino)
                    self.assertEqual((targets / name).lstat().st_mode, before.st_mode)
                    self.assertEqual(source.stat().st_ino, original_inode)
                    self.assertEqual((source / "workspace.yaml").read_bytes(), contents)
            self.assertEqual((targets / "regular-file").read_bytes(), b"existing target")
            self.assertEqual(external.read_bytes(), b"do not overwrite")
            cleanup.rename_noreplace_at(source_fd, sid, target_fd, "unused")
            self.assertFalse(source.exists())
            self.assertEqual((targets / "unused").stat().st_ino, original_inode)
            self.assertEqual((targets / "unused/workspace.yaml").read_bytes(), contents)
        finally:
            os.close(target_fd)
            os.close(source_fd)


if __name__ == "__main__":
    unittest.main()
