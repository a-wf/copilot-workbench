#!/usr/bin/env python3
"""Recoverable cleanup contracts, stdlib-only and confined to scratch HOME.

Fresh ctime cannot be backdated on macOS/Linux. Positive fixtures use old
metadata/mtime and patch only the helper clock two days forward; actual stat,
fingerprint, lsof subprocess, rename, registry and manifest operations remain
real. Unmodified CLI tests also cover fresh sessions with and without the
recent-session opt-in, including a real move/restore without a clock patch.
"""

import contextlib
import datetime
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
import tempfile
import time
import types
import unittest
from unittest import mock

HELPER_PATH = Path(__file__).resolve().parents[1] / "bin/copilot-session-cleanup.py"
SPEC = importlib.util.spec_from_file_location("session_cleanup", HELPER_PATH)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)

# Literal observed bootstrap fixture, deliberately independent of the helper.
OBSERVED_CHECKPOINT_INDEX = (
    b"# Checkpoint History\n\n"
    b"Checkpoints are listed in chronological order. Checkpoint 1 is the oldest, "
    b"higher numbers are more recent.\n\n"
    b"| # | Title | File |\n"
    b"|---|-------|------|\n"
)
SCAFFOLD_PATHS = (
    ".workspace-fork.lock", "files", "research", "checkpoints",
    "checkpoints/index.md",
)


def legacy_workspace_fingerprint(directory):
    """Pre-scaffold three-element hash, not computed by the changed helper."""
    workspace = directory / "workspace.yaml"
    st = workspace.stat()
    material = [
        directory.stat().st_ino, ["workspace.yaml"],
        [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
         hashlib.sha256(workspace.read_bytes()).hexdigest()],
    ]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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

    def scaffold(self, number, fields=None, cwd=None):
        sid, directory = self.session(number, fields=fields, cwd=cwd)
        (directory / ".workspace-fork.lock").write_bytes(b"")
        for name in ("files", "research", "checkpoints"):
            (directory / name).mkdir()
        self.assertEqual(len(OBSERVED_CHECKPOINT_INDEX), 172)
        (directory / "checkpoints/index.md").write_bytes(OBSERVED_CHECKPOINT_INDEX)
        for relative in (".workspace-fork.lock", "checkpoints/index.md"):
            (directory / relative).chmod(0o644)
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            os.utime(directory / relative, (self.old_epoch, self.old_epoch))
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


class TestIncludeRecent(CleanupHomeTestCase):
    def recent_fields(self, epoch=None):
        stamp = datetime.datetime.fromtimestamp(
            self.now - 60 if epoch is None else epoch, datetime.timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {"created_at": stamp, "updated_at": stamp}

    def preview_recent(self, all_directories=False):
        scope = ["--all"] if all_directories else ["--cwd", str(self.cwd)]
        result = self.invoke("preview", *scope, "--include-recent")
        self.assertEqual(result.returncode, 0, result.stderr)
        candidates = {}
        for line in result.stdout.splitlines():
            if line.startswith("CANDIDATE\t"):
                _, sid, fingerprint, marker = line.split("\t")
                self.assertEqual(marker, "include-recent")
                candidates[sid] = fingerprint
        return result, candidates

    def apply_recent(self, candidates):
        return self.invoke(
            "apply", "--all", "--include-recent",
            data="".join("%s\t%s\tinclude-recent\n" % item for item in candidates.items()),
        )

    def test_real_fresh_scaffold_default_keeps_optin_moves_and_restores(self):
        self.clock.stop()
        fresh_epoch = time.time()
        sid, source = self.scaffold(1, fields=self.recent_fields(fresh_epoch))
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            os.utime(source / relative, (fresh_epoch, fresh_epoch))
        original = {
            relative: ((source / relative).stat().st_ino,
                       None if (source / relative).is_dir() else (source / relative).read_bytes())
            for relative in (*SCAFFOLD_PATHS, "workspace.yaml", ".")
        }
        registry_bytes = self.row(sid).encode()
        self.registry.write_bytes(registry_bytes)

        def cli(*arguments, data=""):
            return subprocess.run(
                [sys.executable, str(HELPER_PATH), *self.args(*arguments)],
                env=self.environment, input=data, capture_output=True, text=True, timeout=10,
            )

        default = cli("preview", "--all")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertNotIn("CANDIDATE\t", default.stdout)
        self.assertIn("within the last 24h", default.stdout)
        recent = cli("preview", "--all", "--include-recent")
        self.assertEqual(recent.returncode, 0, recent.stderr)
        lines = [line for line in recent.stdout.splitlines() if line.startswith("CANDIDATE\t")]
        self.assertEqual(len(lines), 1, recent.stdout)
        _, actual_sid, fingerprint, marker = lines[0].split("\t")
        self.assertEqual((actual_sid, marker), (sid, "include-recent"))
        self.assertIn("could start being used right after the open-file", recent.stdout)
        self.assertTrue(source.exists())
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertFalse(self.quarantine.exists(), "both previews must be read-only")

        result = cli("apply", "--all", "--include-recent", data=lines[0].split("\t", 1)[1] + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 1   Skipped: 0   Failed: 0", result.stdout)
        path, manifest = self.manifest()
        self.assertIs(manifest["scope"]["include_recent"], True)
        self.assertEqual(manifest["entries"][0]["fingerprint"], fingerprint)
        destination = path.parent / "sessions" / sid
        self.assertFalse(source.exists())
        self.assertEqual(self.registry.read_bytes(), b"")
        for relative, (inode, data) in original.items():
            self.assertEqual((destination / relative).stat().st_ino, inode)
            if data is not None:
                self.assertEqual((destination / relative).read_bytes(), data)
        result = cli("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(destination.exists())
        for relative, (inode, data) in original.items():
            self.assertEqual((source / relative).stat().st_ino, inode)
            if data is not None:
                self.assertEqual((source / relative).read_bytes(), data)
        self.assertEqual(self.registry.read_bytes(), registry_bytes)
        self.assertEqual(json.loads(path.read_text())["entries"][0]["state"], "restored")

    def test_age_only_optin_preserves_scope_fingerprints_and_default_wire_format(self):
        old, _ = self.session(1)
        recent, recent_dir = self.scaffold(2, fields=self.recent_fields())
        foreign, foreign_dir = self.scaffold(
            3, fields=self.recent_fields(), cwd=self.home / "other",
        )
        default, old_candidates = self.preview()
        self.assertEqual(set(old_candidates), {old})
        self.assertEqual(len(next(
            line for line in default.stdout.splitlines() if line.startswith("CANDIDATE\t")
        ).split("\t")), 3, "default candidate retains the original two payload fields")
        _, local = self.preview_recent()
        _, all_candidates = self.preview_recent(all_directories=True)
        self.assertEqual(set(local), {old, recent})
        self.assertEqual(set(all_candidates), {old, recent, foreign})
        self.assertEqual(local[old], old_candidates[old])
        self.assertEqual(local[recent], all_candidates[recent])
        result = self.apply(old_candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIs(self.manifest()[1]["scope"]["include_recent"], False)
        self.assertTrue(recent_dir.exists())
        self.assertTrue(foreign_dir.exists())

    def test_default_apply_rechecks_recent_age_with_valid_unmarked_optin_fingerprint(self):
        self.clock.stop()
        fresh_epoch = time.time()
        sid, source = self.scaffold(1, fields=self.recent_fields(fresh_epoch))
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            os.utime(source / relative, (fresh_epoch, fresh_epoch))
        original = {
            relative: ((source / relative).stat().st_ino,
                       None if (source / relative).is_dir() else (source / relative).read_bytes())
            for relative in (*SCAFFOLD_PATHS, "workspace.yaml", ".")
        }
        registry_bytes = self.row(sid).encode()
        self.registry.write_bytes(registry_bytes)
        registry_inode = self.registry.stat().st_ino
        _, candidates = self.preview_recent(all_directories=True)
        self.assertEqual(set(candidates), {sid})

        # Deliberately omit the policy marker: valid default-format stdin must
        # reach eligibility revalidation, not fail at the policy parser.
        result = self.invoke("apply", "--all", data="%s\t%s\n" % (sid, candidates[sid]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("Moved: 0   Skipped: 1   Failed: 0", result.stdout)
        self.assertIn("within the last 24h", result.stdout)
        path, manifest = self.manifest()
        self.assertIs(manifest["scope"]["include_recent"], False)
        self.assertEqual(len(manifest["entries"]), 1)
        entry = manifest["entries"][0]
        self.assertEqual(entry["id"], sid)
        self.assertEqual(entry["fingerprint"], candidates[sid])
        self.assertEqual(entry["state"], "skipped")
        self.assertIn("within the last 24h", entry["reason"])
        self.assertEqual(list((path.parent / "sessions").iterdir()), [])
        for relative, (inode, data) in original.items():
            self.assertEqual((source / relative).stat().st_ino, inode)
            if data is not None:
                self.assertEqual((source / relative).read_bytes(), data)
        self.assertEqual(self.registry.stat().st_ino, registry_inode)
        self.assertEqual(self.registry.read_bytes(), registry_bytes)

    def test_optin_relaxes_all_filesystem_mtimes_but_never_future_mtime(self):
        sid, directory = self.scaffold(1)
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            path = directory / relative
            with self.subTest(relative=relative):
                os.utime(path, (self.now - 60, self.now - 60))
                self.assertNotIn(sid, self.preview()[1])
                self.assertIn(sid, self.preview_recent()[1])
                os.utime(path, (self.now + 60, self.now + 60))
                result, candidates = self.preview_recent()
                self.assertNotIn(sid, candidates)
                self.assertIn("timestamp in the future", result.stdout)
                os.utime(path, (self.old_epoch, self.old_epoch))
        self.assertIn(sid, self.preview()[1])

    def test_optin_relaxes_ctime_but_rejects_future_ctime_on_every_item(self):
        sid, directory = self.scaffold(1)
        real_fstat = os.fstat
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            inode = (directory / relative).stat().st_ino
            for changed in (self.now - 60, self.now + 60):
                def changed_stat(fd):
                    st = real_fstat(fd)
                    if st.st_ino != inode:
                        return st
                    fields = {key: getattr(st, key) for key in dir(st) if key.startswith("st_")}
                    fields.update(st_ctime=changed, st_ctime_ns=int(changed * 1e9))
                    return types.SimpleNamespace(**fields)
                with self.subTest(relative=relative, changed=changed), \
                        mock.patch.object(cleanup.os, "fstat", side_effect=changed_stat):
                    result, candidates = self.preview_recent()
                    if changed < self.now:
                        self.assertIn(sid, candidates)
                        self.assertNotIn(sid, self.preview()[1])
                    else:
                        self.assertNotIn(sid, candidates)
                        self.assertIn("timestamp in the future", result.stdout)

    def test_optin_metadata_future_skew_named_and_used_are_not_bypassed(self):
        cutoff = float(int(self.now))
        stamp = lambda epoch: self.recent_fields(epoch)["created_at"]
        rejected = [
            {"created_at": stamp(cutoff + 60), "updated_at": stamp(cutoff + 60)},
            {"created_at": stamp(cutoff), "updated_at": stamp(cutoff + 0.5)},
            {"updated_at": stamp(cutoff - 60 + 1.001)},
            {"updated_at": stamp(cutoff - 60 - 1.001)},
            {"user_named": "true"}, {"name": ""}, {"summary": ""},
            {"summary_count": "1"}, {"fork_count": "1"}, {"id": session_id(999)},
        ]
        with mock.patch.object(cleanup, "now_epoch", return_value=cutoff):
            boundary, _ = self.scaffold(100, fields=self.recent_fields(cutoff))
            skew, _ = self.scaffold(101, fields={
                **self.recent_fields(cutoff - 60), "updated_at": stamp(cutoff - 59),
            })
            for number, fields in enumerate(rejected, 1):
                self.scaffold(number, fields={**self.recent_fields(cutoff - 60), **fields})
            result, candidates = self.preview_recent()
        self.assertEqual(set(candidates), {boundary, skew})
        self.assertIn("timestamp in the future", result.stdout)
        self.assertFalse(self.quarantine.exists())

    def test_optin_rejects_artifacts_partial_scaffold_and_unsafe_links(self):
        control, _ = self.scaffold(100, fields=self.recent_fields())
        mutations = [
            lambda d: (d / "events.jsonl").write_bytes(b""),
            lambda d: (d / "files/note").write_bytes(b""),
            lambda d: (d / ".workspace-fork.lock").write_bytes(b"active"),
            lambda d: (d / "checkpoints/index.md").write_bytes(b"changed"),
            lambda d: (d / "research").rmdir(),
            lambda d: (
                (d / "files").rmdir(), (d / "files").symlink_to(self.cwd, target_is_directory=True)
            ),
            lambda d: os.link(d / "workspace.yaml", self.home / "workspace-link"),
            lambda d: os.link(d / "checkpoints/index.md", self.home / "index-link"),
        ]
        for number, mutate in enumerate(mutations, 1):
            sid, directory = self.scaffold(number, fields=self.recent_fields())
            mutate(directory)
            with self.subTest(number=number):
                self.assertNotIn(sid, self.preview_recent()[1])
        self.assertEqual(set(self.preview_recent()[1]), {control})
        self.assertFalse(self.quarantine.exists())

    def test_optin_keeps_alias_environment_registry_and_owner_vetoes(self):
        sid, directory = self.scaffold(1, fields=self.recent_fields())
        self.assertIn(sid, self.preview_recent()[1])
        self.registry.write_text(self.row(sid, "Chosen alias"), encoding="utf-8")
        self.assertNotIn(sid, self.preview_recent()[1])
        self.registry.unlink()
        with mock.patch.dict(os.environ, {"ACTIVE_CONTEXT": sid.upper()}):
            self.assertNotIn(sid, self.preview_recent()[1])
        target = self.home / "registry"
        target.write_text(self.row(sid), encoding="utf-8")
        self.registry.symlink_to(target)
        self.assertNotIn(sid, self.preview_recent()[1])
        self.registry.unlink()
        real_fstat = os.fstat
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            inode = (directory / relative).stat().st_ino
            def foreign_stat(fd):
                st = real_fstat(fd)
                if st.st_ino != inode:
                    return st
                fields = {key: getattr(st, key) for key in dir(st) if key.startswith("st_")}
                fields["st_uid"] = st.st_uid + 1
                return types.SimpleNamespace(**fields)
            with self.subTest(relative=relative), \
                    mock.patch.object(cleanup.os, "fstat", side_effect=foreign_stat):
                self.assertNotIn(sid, self.preview_recent()[1])
        self.assertEqual(target.read_text(encoding="utf-8"), self.row(sid))
        self.assertIn(sid, self.preview_recent()[1])

    def test_optin_lsof_failclosed_and_apply_rechecks_activity(self):
        sid, directory = self.scaffold(1, fields=self.recent_fields())
        _, frozen = self.preview_recent()
        self.assertEqual(set(frozen), {sid})
        self.lsof.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        self.assertNotIn(sid, self.preview_recent()[1])
        with mock.patch.object(cleanup, "find_lsof", return_value=None):
            self.assertNotIn(sid, self.preview_recent()[1])
        with mock.patch.object(
            cleanup.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 15)
        ):
            self.assertNotIn(sid, self.preview_recent()[1])
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            path = str(directory / relative)
            self.lsof.write_text(
                "#!%s\nimport sys\n"
                "if %r in sys.argv[1:]:\n"
                "    print('p321')\n"
                "    sys.exit(0)\n"
                "sys.exit(1)\n" % (sys.executable, path), encoding="utf-8",
            )
            with self.subTest(open_path=relative):
                result, candidates = self.preview_recent()
                self.assertNotIn(sid, candidates)
                self.assertIn("open by a running process", result.stdout)
        result = self.apply_recent(frozen)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 1", result.stdout)
        self.assertTrue(directory.exists())
        self.assertEqual(self.manifest()[1]["entries"][0]["state"], "skipped")

    def test_policy_mismatch_mixed_and_malformed_requests_create_no_batch(self):
        first, first_dir = self.session(1)
        second, second_dir = self.scaffold(2)
        registry_bytes = (self.row(first) + self.row(second)).encode()
        self.registry.write_bytes(registry_bytes)
        _, normal = self.preview()
        _, recent = self.preview_recent()
        self.assertEqual(normal, recent)
        plain = {sid: "%s\t%s\n" % (sid, fp) for sid, fp in normal.items()}
        marked = {sid: "%s\t%s\tinclude-recent\n" % (sid, fp) for sid, fp in recent.items()}
        cases = [
            ([], marked[first], "same option"),
            (["--include-recent"], plain[first], "same option"),
            ([], plain[first] + marked[second], "same option"),
            (["--include-recent"], marked[first] + plain[second], "same option"),
            (["--include-recent"], marked[first] + marked[first], "duplicate candidate"),
        ]
        for bad in (
            plain[second].rstrip("\n") + "\twrong-policy\n",
            marked[second].rstrip("\n") + "\textra\n",
            plain[second].rstrip("\n") + "\t\n",
        ):
            cases.append((["--include-recent"], marked[first] + bad, "malformed candidate"))
        for flags, data, error in cases:
            with self.subTest(flags=flags, data=data):
                result = self.invoke("apply", "--all", *flags, data=data)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(error, result.stderr)
                self.assertTrue(first_dir.exists())
                self.assertTrue(second_dir.exists())
                self.assertEqual(self.registry.read_bytes(), registry_bytes)
                self.assertFalse(self.quarantine.exists(), "reject the entire request before batch creation")
        self.assertEqual(self.apply_recent({}).returncode, 0)
        self.assertFalse(self.quarantine.exists())

    def test_optin_freezes_ids_and_revalidates_valid_metadata_integrity_alias_and_history(self):
        fixtures = [self.scaffold(n, fields=self.recent_fields()) for n in range(1, 6)]
        self.registry.write_text("".join(self.row(sid) for sid, _ in fixtures), encoding="utf-8")
        _, frozen = self.preview_recent()
        self.assertEqual(len(frozen), 5)
        with (fixtures[1][1] / "workspace.yaml").open("a", encoding="utf-8") as handle:
            handle.write("branch: changed-after-preview\n")
        (fixtures[2][1] / "checkpoints/index.md").chmod(0o600)
        self.registry.write_text("".join(
            self.row(sid, "New alias" if i == 3 else "unnamed")
            for i, (sid, _) in enumerate(fixtures)
        ), encoding="utf-8")
        (fixtures[4][1] / "events.jsonl").write_bytes(b"")
        late, late_dir = self.scaffold(6, fields=self.recent_fields())
        _, changed = self.preview_recent()
        for sid, _ in fixtures[1:3]:
            self.assertIn(sid, changed, "still eligible, but no longer the previewed identity")
            self.assertNotEqual(changed[sid], frozen[sid])
        result = self.apply_recent(frozen)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 1   Skipped: 4", result.stdout)
        self.assertFalse(fixtures[0][1].exists())
        self.assertTrue(all(directory.exists() for _, directory in fixtures[1:]))
        self.assertTrue(late_dir.exists())
        manifest = self.manifest()[1]
        self.assertIs(manifest["scope"]["include_recent"], True)
        self.assertNotIn(late, {entry["id"] for entry in manifest["entries"]})
        self.assertEqual(sum(entry["state"] == "skipped" for entry in manifest["entries"]), 4)
        self.assertIn(self.row(fixtures[3][0], "New alias"), self.registry.read_text(encoding="utf-8"))


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


class TestEmptyScaffoldEligibility(CleanupHomeTestCase):
    def test_observed_scaffold_preview_scope_and_legacy_layout_without_writes(self):
        sid, directory = self.scaffold(1)
        foreign, _ = self.scaffold(2, cwd=self.home / "other")
        legacy, _ = self.session(3)
        named, _ = self.scaffold(4, {"user_named": "true"})
        self.registry.write_text(self.row(sid), encoding="utf-8")
        original = self.registry.read_bytes()
        before = {
            path.relative_to(directory): (path.stat().st_ino, path.stat().st_mtime_ns)
            for path in [directory, *directory.rglob("*")]
        }
        result, candidates = self.preview()
        self.assertEqual(set(candidates), {sid, legacy})
        self.assertIn("2 empty session(s)", result.stdout)
        self.assertEqual(set(self.preview(all_directories=True)[1]), {sid, legacy, foreign})
        self.assertNotIn(named, candidates)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertFalse(self.quarantine.exists())
        self.assertEqual(before, {
            path.relative_to(directory): (path.stat().st_ino, path.stat().st_mtime_ns)
            for path in [directory, *directory.rglob("*")]
        })

    def test_invalid_utf8_metadata_keeps_scaffold_and_workspace_only_in_both_scopes(self):
        for number, layout in enumerate(("scaffold", "workspace-only"), 1):
            with self.subTest(layout=layout):
                fixture = self.scaffold if layout == "scaffold" else self.session
                sid, directory = fixture(number)
                inode = directory.stat().st_ino
                self.registry.write_text(self.row(sid), encoding="utf-8")
                registry_bytes = self.registry.read_bytes()
                _, valid = self.preview()
                self.assertIn(sid, valid, "control fixture must pass scaffold and age guards")
                frozen = {sid: valid[sid]}
                workspace = directory / "workspace.yaml"
                data = workspace.read_bytes()
                self.assertIn(b"host_type: local\n", data)
                corrupt = data.replace(b"host_type: local\n", b"host_type: \xff\n")
                self.assertIn(("cwd: %s\n" % self.cwd).encode("utf-8"), corrupt)
                workspace.write_bytes(corrupt)
                for all_directories in (True, False):
                    with self.subTest(all_directories=all_directories):
                        result, candidates = self.preview(all_directories=all_directories)
                        self.assertNotIn(sid, candidates)
                        # The scaffold path recovers cwd only for filtering.
                        # Legacy workspace-only scoped decoding stays strict.
                        reason = (
                            "metadata not in the expected empty-session form"
                            if all_directories or layout == "scaffold"
                            else "directory unknown (no readable workspace.yaml)"
                        )
                        self.assertIn(reason, result.stdout)
                with mock.patch.object(
                    cleanup, "rename_noreplace_at", wraps=cleanup.rename_noreplace_at
                ) as moves:
                    result = self.apply(frozen)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Moved: 0   Skipped: 1", result.stdout)
                moves.assert_not_called()
                self.assertEqual(directory.stat().st_ino, inode)
                self.assertEqual(workspace.read_bytes(), corrupt)
                self.assertEqual(self.registry.read_bytes(), registry_bytes)
                shutil.rmtree(directory)  # Isolate the next case's diagnostic.

    def test_exact_scaffold_with_nonempty_or_named_metadata_is_kept_without_move(self):
        used_timestamp = datetime.datetime.fromtimestamp(
            cleanup.parse_timestamp(self.old) + 2, datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        variations = (
            ("name", "", "has a native name or summary"),
            ("summary_count", "1", "has a native name or summary"),
            ("updated_at", used_timestamp, "updated after creation (the session was used)"),
            ("user_named", "true", "has a native name or summary"),
        )
        for number, (key, value, reason) in enumerate(variations, 1):
            with self.subTest(key=key, value=value):
                sid, directory = self.scaffold(number)
                inode = directory.stat().st_ino
                self.registry.write_text(self.row(sid), encoding="utf-8")
                registry_bytes = self.registry.read_bytes()
                _, valid = self.preview()
                self.assertIn(sid, valid, "control fixture must pass scaffold and age guards")
                frozen = {sid: valid[sid]}
                workspace = directory / "workspace.yaml"
                lines = workspace.read_text(encoding="utf-8").splitlines()
                replacement = "%s: %s" % (key, value)
                if any(line.startswith(key + ":") for line in lines):
                    lines = [replacement if line.startswith(key + ":") else line for line in lines]
                else:
                    lines.append(replacement)
                workspace.write_text("\n".join(lines) + "\n", encoding="utf-8")
                workspace_bytes = workspace.read_bytes()
                for all_directories in (True, False):
                    result, candidates = self.preview(all_directories=all_directories)
                    self.assertNotIn(sid, candidates)
                    self.assertIn(reason, result.stdout)
                self.assertEqual((directory / ".workspace-fork.lock").read_bytes(), b"")
                self.assertEqual(
                    (directory / "checkpoints/index.md").read_bytes(), OBSERVED_CHECKPOINT_INDEX
                )
                with mock.patch.object(
                    cleanup, "rename_noreplace_at", wraps=cleanup.rename_noreplace_at
                ) as moves:
                    result = self.apply(frozen)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Moved: 0   Skipped: 1", result.stdout)
                moves.assert_not_called()
                self.assertEqual(directory.stat().st_ino, inode)
                self.assertEqual(workspace.read_bytes(), workspace_bytes)
                self.assertEqual(self.registry.read_bytes(), registry_bytes)
                shutil.rmtree(directory)  # Isolate the next case's diagnostic.

    def test_nonempty_lock_and_nonexact_checkpoint_index_are_kept(self):
        variations = [
            (".workspace-fork.lock", b"\n"),
            ("checkpoints/index.md", b""),
            ("checkpoints/index.md", OBSERVED_CHECKPOINT_INDEX + b"| 1 | Used | 1.md |\n"),
            ("checkpoints/index.md", b"!" + OBSERVED_CHECKPOINT_INDEX[1:]),
            ("checkpoints/index.md", OBSERVED_CHECKPOINT_INDEX.replace(b"\n", b"\r\n")),
        ]
        for number, (relative, data) in enumerate(variations, 1):
            with self.subTest(relative=relative, data=data):
                sid, directory = self.scaffold(number)
                (directory / relative).write_bytes(data)
                result, candidates = self.preview()
                self.assertNotIn(sid, candidates)
                self.assertIn("has session history or other files", result.stdout)
        self.assertFalse(self.quarantine.exists())

    def test_nonempty_directories_extra_checkpoints_and_history_are_kept(self):
        for number, relative in enumerate((
            "files/.hidden", "research/result.md", "checkpoints/1.md",
            "session.db", "events.jsonl", "unknown", "checkpoints/.hidden",
        ), 1):
            with self.subTest(relative=relative):
                sid, directory = self.scaffold(number)
                (directory / relative).write_bytes(b"")
                self.assertNotIn(sid, self.preview()[1])

    def test_every_partial_scaffold_is_kept(self):
        for number, relative in enumerate((*SCAFFOLD_PATHS, "workspace.yaml"), 1):
            with self.subTest(missing=relative):
                sid, directory = self.scaffold(number)
                path = directory / relative
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                self.assertNotIn(sid, self.preview(all_directories=True)[1])

    def test_symlink_files_directories_and_hardlinks_are_kept(self):
        for number, relative in enumerate((*SCAFFOLD_PATHS, "workspace.yaml"), 1):
            with self.subTest(symlink=relative):
                sid, directory = self.scaffold(number)
                path = directory / relative
                target = self.home / ("outside-%d" % number)
                is_directory = path.is_dir()
                path.rename(target)
                path.symlink_to(target, target_is_directory=is_directory)
                original = target.stat().st_ino
                self.assertNotIn(sid, self.preview(all_directories=True)[1])
                self.assertEqual(target.stat().st_ino, original)
                self.assertTrue(path.is_symlink())
        for number, relative in enumerate((
            ".workspace-fork.lock", "checkpoints/index.md", "workspace.yaml",
        ), 20):
            with self.subTest(hardlink=relative):
                sid, directory = self.scaffold(number)
                os.link(directory / relative, self.home / ("hardlink-%d" % number))
                self.assertNotIn(sid, self.preview()[1])

    def test_wrong_file_or_directory_types_are_kept_without_opening_fifo(self):
        for number, relative in enumerate(SCAFFOLD_PATHS, 1):
            with self.subTest(relative=relative):
                sid, directory = self.scaffold(number)
                path = directory / relative
                if path.is_dir():
                    shutil.rmtree(path)
                    path.write_bytes(b"")
                else:
                    path.unlink()
                    os.mkfifo(path)
                real_open = os.open
                def guarded_open(name, flags, *args, **kwargs):
                    self.assertNotEqual(
                        (name, kwargs.get("dir_fd") is not None),
                        (path.name, True), "wrong-type scaffold item must not be opened",
                    )
                    return real_open(name, flags, *args, **kwargs)
                with mock.patch.object(cleanup.os, "open", side_effect=guarded_open):
                    self.assertNotIn(sid, self.preview()[1])

    def test_nonowner_stat_of_every_scaffold_item_is_kept(self):
        sid, directory = self.scaffold(1)
        real_fstat = os.fstat
        for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
            inode = (directory / relative).stat().st_ino
            def foreign_stat(fd):
                st = real_fstat(fd)
                if st.st_ino != inode:
                    return st
                fields = {key: getattr(st, key) for key in dir(st) if key.startswith("st_")}
                fields["st_uid"] = st.st_uid + 1
                return types.SimpleNamespace(**fields)
            with self.subTest(relative=relative), \
                    mock.patch.object(cleanup.os, "fstat", side_effect=foreign_stat):
                result, candidates = self.preview()
                self.assertNotIn(sid, candidates)
                self.assertIn("unsafe directory or file", result.stdout)
        self.assertIn(sid, self.preview()[1])

    def test_all_scaffold_paths_enforce_exact_24h_boundary_and_future_mtime(self):
        sid, directory = self.scaffold(1)
        cutoff = float(int(self.now - cleanup.MIN_AGE_SECONDS))
        with mock.patch.object(cleanup, "now_epoch", return_value=cutoff + cleanup.MIN_AGE_SECONDS):
            for relative in (*SCAFFOLD_PATHS, "workspace.yaml", "."):
                path = directory / relative
                with self.subTest(relative=relative):
                    os.utime(path, (cutoff, cutoff))
                    self.assertIn(sid, self.preview()[1], "exactly 24h is old enough")
                    for modified in (cutoff + 0.5, self.now + 60):
                        os.utime(path, (modified, modified))
                        result, candidates = self.preview()
                        self.assertNotIn(sid, candidates)
                        self.assertIn("within the last 24h", result.stdout)
                    os.utime(path, (self.old_epoch, self.old_epoch))

    def test_nested_ctime_alone_enforces_recent_and_future_veto(self):
        sid, directory = self.scaffold(1)
        real_fstat = os.fstat
        for relative in SCAFFOLD_PATHS:
            inode = (directory / relative).stat().st_ino
            for changed in (self.now - 60, self.now + 60):
                def recent_stat(fd):
                    st = real_fstat(fd)
                    if st.st_ino != inode:
                        return st
                    fields = {key: getattr(st, key) for key in dir(st) if key.startswith("st_")}
                    fields.update(st_ctime=changed, st_ctime_ns=int(changed * 1e9))
                    return types.SimpleNamespace(**fields)
                with self.subTest(relative=relative, changed=changed), \
                        mock.patch.object(cleanup.os, "fstat", side_effect=recent_stat):
                    result, candidates = self.preview()
                    self.assertNotIn(sid, candidates)
                    self.assertIn("within the last 24h", result.stdout)

    def test_real_cli_valid_but_fresh_scaffold_is_too_recent_not_history(self):
        _, directory = self.scaffold(1)
        result = subprocess.run(
            [sys.executable, str(HELPER_PATH), *self.args("preview", "--all")],
            env=self.environment, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CANDIDATE\t", result.stdout)
        self.assertIn("created, updated, or modified within the last 24h", result.stdout)
        self.assertNotIn("has session history", result.stdout)
        self.assertTrue(directory.exists())
        self.assertFalse(self.quarantine.exists())

    def test_lsof_argv_contains_every_item_and_each_open_item_vetoes(self):
        sid, directory = self.scaffold(1)
        paths = {str(directory / relative) for relative in (*SCAFFOLD_PATHS, "workspace.yaml", ".")}
        real_run = subprocess.run
        with mock.patch.object(cleanup.subprocess, "run", wraps=real_run) as calls:
            self.assertIn(sid, self.preview()[1])
        calls.assert_called_once()
        argv = calls.call_args.args[0]
        self.assertEqual(set(argv[argv.index("--") + 1:]), paths)
        for path in sorted(paths):
            with self.subTest(open_path=path):
                self.lsof.write_text(
                    "#!%s\nimport sys\n"
                    "if %r in sys.argv[1:]:\n"
                    "    print('p321')\n"
                    "    sys.exit(0)\n"
                    "sys.exit(1)\n" % (sys.executable, path), encoding="utf-8",
                )
                result, candidates = self.preview()
                self.assertNotIn(sid, candidates)
                self.assertIn("open by a running process", result.stdout)

    def test_scaffold_activity_unknown_missing_and_timeout_are_failclosed(self):
        sid, _ = self.scaffold(1)
        self.lsof.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        result, candidates = self.preview()
        self.assertNotIn(sid, candidates)
        self.assertIn("activity check unknown", result.stdout)
        with mock.patch.object(cleanup, "find_lsof", return_value=None):
            self.assertNotIn(sid, self.preview()[1])
        with mock.patch.object(
            cleanup.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 15)
        ):
            self.assertNotIn(sid, self.preview()[1])

    def test_scaffold_scans_use_relative_nofollow_fds_and_fd_directory_lists(self):
        sid, _ = self.scaffold(1)
        parent = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(os.close, parent)
        real_open, real_listdir = os.open, os.listdir
        with mock.patch.object(cleanup.os, "open", wraps=real_open) as opens, \
                mock.patch.object(cleanup.os, "listdir", wraps=real_listdir) as lists:
            _, details = cleanup.fingerprint_dir(sid, parent)
        self.assertIsNone(details["scaffold_problem"])
        expected = {".workspace-fork.lock", "files", "research", "checkpoints", "index.md"}
        seen = set()
        for call in opens.call_args_list:
            name, flags = call.args[:2]
            if name in expected:
                seen.add(name)
                self.assertIsInstance(call.kwargs.get("dir_fd"), int)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
        self.assertEqual(seen, expected)
        self.assertTrue(lists.call_args_list)
        self.assertTrue(all(isinstance(call.args[0], int) for call in lists.call_args_list))

    def test_file_identity_swap_between_lstat_and_open_is_kept(self):
        for number, relative in enumerate((".workspace-fork.lock", "checkpoints/index.md"), 1):
            sid, directory = self.scaffold(number)
            path = directory / relative
            replacement = self.home / ("replacement-%d" % number)
            replacement.write_bytes(path.read_bytes())
            real_open = os.open
            swapped = []
            def swap_before_open(name, flags, *args, **kwargs):
                if name == path.name and kwargs.get("dir_fd") is not None and not swapped:
                    path.unlink()
                    replacement.rename(path)
                    swapped.append(True)
                return real_open(name, flags, *args, **kwargs)
            with self.subTest(relative=relative), \
                    mock.patch.object(cleanup.os, "open", side_effect=swap_before_open):
                result, candidates = self.preview()
            self.assertEqual(swapped, [True])
            self.assertNotIn(sid, candidates)
            self.assertIn("changed while being inspected", result.stdout)
            shutil.rmtree(directory)

    def test_index_mutation_during_fd_read_is_kept(self):
        sid, directory = self.scaffold(1)
        path = directory / "checkpoints/index.md"
        inode = path.stat().st_ino
        real_read, real_fstat = os.read, os.fstat
        mutated = []
        def mutate_after_read(fd, count):
            data = real_read(fd, count)
            if real_fstat(fd).st_ino == inode and not mutated:
                path.write_bytes(b"!" + OBSERVED_CHECKPOINT_INDEX[1:])
                mutated.append(True)
            return data
        with mock.patch.object(cleanup.os, "read", side_effect=mutate_after_read):
            result, candidates = self.preview()
        self.assertEqual(mutated, [True])
        self.assertNotIn(sid, candidates)
        self.assertIn("changed while being inspected", result.stdout)

    def test_directory_mutation_between_fd_lists_is_kept(self):
        sid, directory = self.scaffold(1)
        inode = (directory / "files").stat().st_ino
        real_listdir, real_fstat = os.listdir, os.fstat
        mutated = []
        def mutate_after_list(fd):
            entries = real_listdir(fd)
            if isinstance(fd, int) and real_fstat(fd).st_ino == inode and not mutated:
                (directory / "files/.activity").write_bytes(b"")
                mutated.append(True)
            return entries
        with mock.patch.object(cleanup.os, "listdir", side_effect=mutate_after_list):
            result, candidates = self.preview()
        self.assertEqual(mutated, [True])
        self.assertNotIn(sid, candidates)
        self.assertIn("changed while being inspected", result.stdout)

    def test_every_scaffold_item_integrity_change_alters_fingerprint(self):
        sid, directory = self.scaffold(1)
        workspace_stat = (directory / "workspace.yaml").stat()
        original_directory_stat = directory.stat()
        _, candidates = self.preview()
        for relative in SCAFFOLD_PATHS:
            path = directory / relative
            mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(relative=relative):
                path.chmod(mode ^ stat.S_IWGRP)
                current = self.preview()[1][sid]
                self.assertNotEqual(current, candidates[sid])
                self.assertEqual((directory / "workspace.yaml").stat(), workspace_stat)
                self.assertEqual(directory.stat().st_mtime_ns, original_directory_stat.st_mtime_ns)
                self.assertEqual(directory.stat().st_ctime_ns, original_directory_stat.st_ctime_ns)
                path.chmod(mode)
                candidates = self.preview()[1]


class TestEmptyScaffoldTransactions(CleanupHomeTestCase):
    def test_real_scaffold_preview_apply_restore_preserves_all_inodes_and_bytes(self):
        sid, source = self.scaffold(1)
        original = {
            relative: ((source / relative).stat().st_ino,
                       None if (source / relative).is_dir() else (source / relative).read_bytes())
            for relative in (*SCAFFOLD_PATHS, "workspace.yaml", ".")
        }
        unrelated = "unrelated|Keep alias|timestamp|/other\n"
        self.registry.write_text(self.row(sid) + unrelated, encoding="utf-8")
        _, candidates = self.preview()
        self.assertEqual(set(candidates), {sid})
        real_rename = cleanup.rename_noreplace_at
        with mock.patch.object(cleanup, "rename_noreplace_at", wraps=real_rename) as moves:
            result = self.apply(candidates)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Moved: 1   Skipped: 0", result.stdout)
            path, manifest = self.manifest()
            destination = path.parent / "sessions" / sid
            self.assertFalse(source.exists())
            self.assertEqual(self.registry.read_text(encoding="utf-8"), unrelated)
            entry = manifest["entries"][0]
            self.assertEqual(entry["fingerprint"], candidates[sid])
            self.assertEqual(entry["state"], "moved")
            for relative, (inode, data) in original.items():
                self.assertEqual((destination / relative).stat().st_ino, inode)
                if data is not None:
                    self.assertEqual((destination / relative).read_bytes(), data)
            result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(moves.call_count, 2, "real forward and restore no-replace calls")
        self.assertFalse(destination.exists())
        for relative, (inode, data) in original.items():
            self.assertEqual((source / relative).stat().st_ino, inode)
            if data is not None:
                self.assertEqual((source / relative).read_bytes(), data)
        self.assertIn(self.row(sid), self.registry.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text())["entries"][0]["state"], "restored")

    def test_original_workspace_only_manifest_fingerprint_restores(self):
        sid, source = self.session(1)
        old_fingerprint = legacy_workspace_fingerprint(source)
        self.assertEqual(self.preview()[1][sid], old_fingerprint)
        batch = "20261112-180000-0123abcd"
        sessions = self.quarantine / batch / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        self.quarantine.chmod(0o700)
        sessions.parent.chmod(0o700)
        source_fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        destination_fd = os.open(sessions, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            cleanup.rename_noreplace_at(source_fd, sid, destination_fd, sid)
        finally:
            os.close(destination_fd)
            os.close(source_fd)
        manifest = {
            "version": 1, "batch": batch, "created_at": self.old,
            "session_state_dir": str(self.state), "sessions_file": str(self.registry),
            "scope": {"all": True, "cwd": None},
            "entries": [{
                "id": sid, "fingerprint": old_fingerprint, "state": "moved",
                "reason": None, "registry": "removed",
                "registry_rows": [self.row(sid).rstrip("\n")],
            }],
        }
        path = sessions.parent / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(legacy_workspace_fingerprint(sessions / sid), old_fingerprint)
        result = self.invoke("restore", "--batch", batch, data="yes\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(source.exists())
        self.assertFalse((sessions / sid).exists())
        self.assertEqual(legacy_workspace_fingerprint(source), old_fingerprint)
        self.assertEqual(self.registry.read_text(encoding="utf-8"), self.row(sid))

    def test_preview_to_apply_scaffold_content_and_integrity_mutations_skip(self):
        fixtures = [self.scaffold(number) for number in range(1, 8)]
        original = "".join(self.row(sid) for sid, _ in fixtures).encode()
        self.registry.write_bytes(original)
        _, candidates = self.preview()
        self.assertEqual(len(candidates), len(fixtures))
        (fixtures[0][1] / ".workspace-fork.lock").write_bytes(b"active")
        (fixtures[1][1] / "checkpoints/index.md").write_bytes(b"!" + OBSERVED_CHECKPOINT_INDEX[1:])
        (fixtures[2][1] / "files/.activity").write_bytes(b"")
        (fixtures[3][1] / "research/result.md").write_bytes(b"")
        (fixtures[4][1] / "checkpoints/1.md").write_bytes(b"")
        index = fixtures[5][1] / "checkpoints/index.md"
        index.chmod(0o600)
        lock = fixtures[6][1] / ".workspace-fork.lock"
        replacement = self.home / "new-lock"
        replacement.write_bytes(b"")
        lock.unlink()
        replacement.rename(lock)
        # chmod keeps the exact layout valid, but must change its fingerprint.
        self.assertNotEqual(self.preview()[1][fixtures[5][0]], candidates[fixtures[5][0]])
        with mock.patch.object(cleanup, "rename_noreplace_at", wraps=cleanup.rename_noreplace_at) as moves:
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 7", result.stdout)
        moves.assert_not_called()
        self.assertTrue(all(directory.exists() for _, directory in fixtures))
        self.assertEqual(self.registry.read_bytes(), original)
        path, manifest = self.manifest()
        self.assertTrue(all(entry["state"] == "skipped" for entry in manifest["entries"]))
        self.assertEqual(list((path.parent / "sessions").iterdir()), [])

    def test_scaffold_activity_change_after_preview_skips_apply(self):
        sid, source = self.scaffold(1)
        original = self.row(sid).encode()
        self.registry.write_bytes(original)
        _, candidates = self.preview()
        self.assertEqual(set(candidates), {sid})
        self.lsof.write_text("#!/bin/sh\nprintf 'p321\\n'\nexit 0\n", encoding="utf-8")
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 1", result.stdout)
        self.assertTrue(source.exists())
        self.assertEqual(self.registry.read_bytes(), original)

    def test_scaffold_mutation_under_registry_lock_skips_final_revalidation(self):
        sid, source = self.scaffold(1)
        self.registry.write_text(self.row(sid), encoding="utf-8")
        _, candidates = self.preview()
        real_enter = cleanup.RegistryLock.__enter__
        def mutate_on_lock(lock):
            result = real_enter(lock)
            (source / "checkpoints/index.md").chmod(0o600)
            return result
        with mock.patch.object(cleanup.RegistryLock, "__enter__", new=mutate_on_lock), \
                mock.patch.object(cleanup, "rename_noreplace_at", wraps=cleanup.rename_noreplace_at) as moves:
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 1", result.stdout)
        moves.assert_not_called()
        self.assertTrue(source.exists())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), self.row(sid))

    def test_real_postmove_scaffold_mutations_reverse_rename_and_keep_registry(self):
        mutations = [
            (".workspace-fork.lock", b"active", False),
            ("checkpoints/index.md", b"!" + OBSERVED_CHECKPOINT_INDEX[1:], False),
            ("files/.activity", b"file activity", True),
            ("research/result.md", b"research activity", True),
            ("checkpoints/1.md", b"checkpoint activity", True),
            ("checkpoints/index.md", None, False),
        ]
        fixtures = [self.scaffold(number) for number in range(1, len(mutations) + 1)]
        plans = {sid: mutation for (sid, _), mutation in zip(fixtures, mutations)}
        inodes = {sid: source.stat().st_ino for sid, source in fixtures}
        original = "".join(self.row(sid) for sid, _ in fixtures).encode()
        self.registry.write_bytes(original)
        _, candidates = self.preview()
        self.assertEqual(len(candidates), len(fixtures))
        state_inode = self.state.stat().st_ino
        real_rename = cleanup.rename_noreplace_at
        moves = []
        def mutate_after_forward(src_fd, src_name, dst_fd, dst_name):
            src_parent, dst_parent = os.fstat(src_fd).st_ino, os.fstat(dst_fd).st_ino
            result = real_rename(src_fd, src_name, dst_fd, dst_name)
            moves.append((src_name, src_parent, dst_parent))
            if src_parent == state_inode:
                directory_fd = os.open(
                    dst_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd
                )
                relative, data, create = plans[src_name]
                try:
                    flags = os.O_NOFOLLOW | (os.O_RDONLY if data is None else os.O_WRONLY | os.O_TRUNC)
                    if create:
                        flags |= os.O_CREAT | os.O_EXCL
                    fd = os.open(relative, flags, 0o600, dir_fd=directory_fd)
                    with os.fdopen(fd, "rb" if data is None else "wb") as handle:
                        if data is None:
                            os.fchmod(handle.fileno(), 0o600)
                        else:
                            handle.write(data)
                finally:
                    os.close(directory_fd)
            return result
        with mock.patch.object(cleanup, "rename_noreplace_at", side_effect=mutate_after_forward):
            result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 0   Skipped: 6", result.stdout)
        self.assertEqual(len(moves), 2 * len(fixtures))
        for (sid, source), (relative, data, _) in zip(fixtures, mutations):
            pair = [move for move in moves if move[0] == sid]
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0][1:], pair[1][1:][::-1])
            self.assertEqual(pair[0][1], state_inode)
            self.assertEqual(source.stat().st_ino, inodes[sid])
            if data is not None:
                self.assertEqual((source / relative).read_bytes(), data)
            else:
                self.assertEqual(stat.S_IMODE((source / relative).stat().st_mode), 0o600)
        self.assertEqual(self.registry.read_bytes(), original)
        path, manifest = self.manifest()
        self.assertTrue(all(entry["state"] == "skipped" for entry in manifest["entries"]))
        self.assertTrue(all("moved back" in entry["reason"] for entry in manifest["entries"]))
        self.assertEqual(list((path.parent / "sessions").iterdir()), [])

    def test_changed_quarantine_scaffold_content_links_and_integrity_refuse_restore(self):
        fixtures = [self.scaffold(number) for number in range(1, 8)]
        self.registry.write_text("".join(self.row(sid) for sid, _ in fixtures), encoding="utf-8")
        _, candidates = self.preview()
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Moved: 7   Skipped: 0", result.stdout)
        path, manifest = self.manifest()
        destinations = [path.parent / "sessions" / sid for sid, _ in fixtures]
        (destinations[0] / ".workspace-fork.lock").write_bytes(b"active")
        (destinations[1] / "checkpoints/index.md").write_bytes(b"!" + OBSERVED_CHECKPOINT_INDEX[1:])
        (destinations[2] / "files/.activity").write_bytes(b"")
        (destinations[3] / "checkpoints/index.md").chmod(0o600)
        os.link(destinations[4] / ".workspace-fork.lock", self.home / "quarantine-hardlink")
        index = destinations[5] / "checkpoints/index.md"
        external = self.home / "quarantine-index"
        index.rename(external)
        index.symlink_to(external)
        (destinations[6] / "checkpoints/index.md").unlink()
        result = self.invoke("restore", "--batch", manifest["batch"], data="yes\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("quarantined contents changed"), len(fixtures))
        self.assertTrue(all(not source.exists() for _, source in fixtures))
        self.assertTrue(all(destination.exists() for destination in destinations))
        self.assertEqual(self.registry.read_bytes(), b"")
        self.assertEqual(external.read_bytes(), OBSERVED_CHECKPOINT_INDEX)

    def test_restore_rechecks_scaffold_mutation_after_confirmation(self):
        sid, source = self.scaffold(1)
        self.registry.write_text(self.row(sid), encoding="utf-8")
        _, candidates = self.preview()
        result = self.apply(candidates)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        path, manifest = self.manifest()
        destination = path.parent / "sessions" / sid
        def mutate_on_confirm(_):
            (destination / "checkpoints/index.md").chmod(0o600)
            return True
        with mock.patch.object(cleanup, "confirm", side_effect=mutate_on_confirm), \
                mock.patch.object(cleanup, "rename_noreplace_at", wraps=cleanup.rename_noreplace_at) as moves:
            result = self.invoke("restore", "--batch", manifest["batch"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("quarantined contents changed", result.stdout)
        moves.assert_not_called()
        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(self.registry.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
