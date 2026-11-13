#!/usr/bin/env python3
"""
Focused regression tests for bin/copilot-s, covering the reviewer findings
fixed in this pass:

  1. copilot-task-report.py helper resolution must follow copilot-s's own
     (symlink-resolved) install location — correct for both the default
     symlink install and `--copy`, and for any custom `--prefix` — with a
     COPILOT_TASK_REPORT_HELPER env override taking priority, and a
     sensible ~/.local/bin fallback.
  2. `copilot-s --help` (and `--version`) must print and exit immediately:
     no prompting, no session listing, no launching of `copilot`.
  5. `scripts/install.sh --help` / `scripts/uninstall.sh --help` must print
     their full header comment, not a truncated fixed line range.

These tests drive the real scripts via subprocess against scratch HOME
directories (never the real ~), mirroring the installer smoke tests in
.github/workflows/ci.yml but as reusable, individually-diagnosable unit
tests. Self-contained: stdlib-only (unittest), no network, no writes
outside per-test temp directories.
"""

import datetime
import fcntl
import json
import os
import pty
import select
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPILOT_S = os.path.join(REPO_ROOT, "bin", "copilot-s")
TASK_HELPER = os.path.join(REPO_ROOT, "bin", "copilot-task-report.py")
CLEANUP_HELPER = os.path.join(REPO_ROOT, "bin", "copilot-session-cleanup.py")
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")
UNINSTALL_SH = os.path.join(REPO_ROOT, "scripts", "uninstall.sh")


def run(cmd, env=None, timeout=15, **kwargs):
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        **kwargs,
    )


class BaseHomeTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="copilot-s-test-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def env(self, extra=None):
        e = dict(os.environ)
        e["HOME"] = self.home
        for key in (
            "COPILOT_TASK_REPORTS_DIR", "COPILOT_TASK_REPORT_HELPER",
            "COPILOT_SESSION_CLEANUP_HELPER",
        ):
            e.pop(key, None)
        # Any real task-report helper reached by a shell subprocess must
        # remain network-free, even if the ambient environment enables
        # automatic official-price refreshes.
        e["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"
        # Never let a real Copilot CLI be found/launched from these tests.
        e["PATH"] = "/usr/bin:/bin"
        if extra:
            e.update(extra)
        return e


class TestHelpAndVersionAreSafe(BaseHomeTestCase):
    """--help/--version must exit(0) immediately: no prompting, no launch
    of `copilot`, no session-manager side effects requiring a tty."""

    def test_help_exits_zero_and_prints_usage(self):
        result = run([COPILOT_S, "--help"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("copilot-s", result.stdout)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("--report", result.stdout)
        self.assertIn("Copilot launches use Auto with the Balance tier", result.stdout)

    def test_short_help_flag_behaves_the_same(self):
        result = run([COPILOT_S, "-h"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)

    def test_help_takes_priority_over_other_flags(self):
        # --help anywhere in argv must win, even combined with other flags.
        result = run(
            [COPILOT_S, "--all", "--help"], env=self.env(), stdin=subprocess.DEVNULL
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)

    def test_version_exits_zero_and_prints_version(self):
        result = run([COPILOT_S, "--version"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^copilot-s \d+\.\d+\.\d+$")

    def test_help_does_not_touch_session_or_marker_state(self):
        result = run([COPILOT_S, "--help"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.home, ".copilot-sessions")),
            "--help must not create/list session state",
        )

    def test_help_has_no_otel_filesystem_side_effects(self):
        # --help/--version must not create ~/.copilot/otel (deferred until
        # a real session-manager invocation via setup_otel()).
        result = run([COPILOT_S, "--help"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.home, ".copilot", "otel")),
            "--help must not create the OTEL export directory",
        )

    def test_version_has_no_otel_filesystem_side_effects(self):
        result = run([COPILOT_S, "--version"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.home, ".copilot", "otel")),
            "--version must not create the OTEL export directory",
        )

    def test_report_flag_sets_up_otel_for_real_invocation(self):
        # A real (non-informational) invocation must still get OTEL telemetry
        # set up, even though it exits early via the --report path.
        os.makedirs(os.path.join(self.home, ".copilot", "task-reports"), exist_ok=True)
        result = run(
            [COPILOT_S, "--report", "ABC-123"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, ".copilot", "otel")),
            "a real invocation must still create the OTEL export directory",
        )

    def test_task_flag_is_an_alias_for_report_flag(self):
        # --task TASK_ID must behave identically to --report TASK_ID.
        os.makedirs(os.path.join(self.home, ".copilot", "task-reports"), exist_ok=True)
        result = run(
            [COPILOT_S, "--task", "ABC-123"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, ".copilot", "otel")),
            "--task must set up OTEL just like --report",
        )

    def test_task_equals_form_is_accepted(self):
        os.makedirs(os.path.join(self.home, ".copilot", "task-reports"), exist_ok=True)
        result = run(
            [COPILOT_S, "--task=ABC-123"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, ".copilot", "otel")),
            "--task=ID must be parsed the same as --task ID",
        )

    def test_help_mentions_task_alias(self):
        result = run([COPILOT_S, "--help"], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--task", result.stdout)

    def test_task_flag_does_not_swallow_a_following_flag_as_its_value(self):
        # `copilot-s --task --all` must not silently treat "--all" as the
        # task ID (and thereby drop it): a real task ID can never start
        # with '-', so the next token must only be consumed as the value
        # when it doesn't itself look like a flag.
        os.makedirs(os.path.join(self.home, ".copilot", "task-reports"), exist_ok=True)
        result = run(
            [COPILOT_S, "--task", "--all"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        # No value was supplied (the next token was rejected as a value),
        # so this must behave like a bare/missing task ID: a clear usage
        # error, not a silent swallow-and-continue.
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage", result.stderr)


class TestHelperResolution(BaseHomeTestCase):
    """copilot-task-report.py must be found relative to copilot-s's own
    resolved location, regardless of symlink vs --copy install mode or
    --prefix, with an env override taking priority."""

    def _resolved_helper_path(self, copilot_s_path, extra_env=None, variable="TASK_REPORT_HELPER"):
        # `--version` exits before any task-report work, so use `bash -x`
        # to observe the TASK_REPORT_HELPER assignment without needing a
        # tty or a real `copilot` binary on PATH.
        result = run(
            ["bash", "-x", copilot_s_path, "--version"],
            env=self.env(extra_env),
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in result.stderr.splitlines():
            line = line.strip()
            if line.startswith("+ " + variable + "="):
                return line.split("=", 1)[1]
        self.fail(f"{variable} assignment not observed in:\n{result.stderr}")

    def test_symlink_install_resolves_helper_next_to_repo(self):
        run([INSTALL_SH], env=self.env(), check=True)
        installed = os.path.join(self.home, ".local", "bin", "copilot-s")
        self.assertTrue(os.path.islink(installed))
        resolved = self._resolved_helper_path(installed)
        self.assertEqual(resolved, TASK_HELPER)

    def test_copy_install_resolves_helper_as_sibling_copy(self):
        run([INSTALL_SH, "--copy"], env=self.env(), check=True)
        installed = os.path.join(self.home, ".local", "bin", "copilot-s")
        self.assertFalse(os.path.islink(installed))
        resolved = self._resolved_helper_path(installed)
        expected = os.path.realpath(
            os.path.join(self.home, ".local", "bin", "copilot-task-report.py")
        )
        self.assertEqual(os.path.realpath(resolved), expected)
        self.assertTrue(os.path.exists(resolved))

    def test_custom_prefix_resolves_helper_alongside_it(self):
        custom_prefix = os.path.join(self.home, "custom", "tools")
        run([INSTALL_SH, "--prefix", custom_prefix], env=self.env(), check=True)
        installed = os.path.join(custom_prefix, "copilot-s")
        self.assertTrue(os.path.islink(installed))
        resolved = self._resolved_helper_path(installed)
        self.assertEqual(resolved, TASK_HELPER)

    def test_env_override_wins_over_sibling_resolution(self):
        run([INSTALL_SH], env=self.env(), check=True)
        installed = os.path.join(self.home, ".local", "bin", "copilot-s")
        override = "/tmp/some-other-copilot-task-report.py"
        resolved = self._resolved_helper_path(
            installed, extra_env={"COPILOT_TASK_REPORT_HELPER": override}
        )
        self.assertEqual(resolved, override)

    def test_fallback_to_local_bin_when_no_sibling_helper(self):
        # Run copilot-s directly from the repo checkout's bin/ dir, but
        # with a decoy helper only present at ~/.local/bin — since the
        # repo bin/ *does* have a real helper next to it, simulate the
        # "no sibling helper" case by invoking a standalone copy placed
        # somewhere with no copilot-task-report.py alongside it.
        standalone_dir = os.path.join(self.home, "standalone")
        os.makedirs(standalone_dir)
        standalone_copilot_s = os.path.join(standalone_dir, "copilot-s")
        shutil.copyfile(COPILOT_S, standalone_copilot_s)
        os.chmod(standalone_copilot_s, os.stat(standalone_copilot_s).st_mode | stat.S_IEXEC)

        fallback_dir = os.path.join(self.home, ".local", "bin")
        os.makedirs(fallback_dir)
        fallback_helper = os.path.join(fallback_dir, "copilot-task-report.py")
        with open(fallback_helper, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n")

        resolved = self._resolved_helper_path(standalone_copilot_s)
        self.assertEqual(resolved, fallback_helper)


class TestCleanupHelperDistribution(BaseHomeTestCase):
    _resolved_helper_path = TestHelperResolution._resolved_helper_path

    def test_cleanup_helper_install_modes_and_uninstall_preserve_quarantine(self):
        quarantine = os.path.join(self.home, ".copilot-s-quarantine")
        os.makedirs(quarantine, mode=0o700)
        sentinel = os.path.join(quarantine, "recoverable-session")
        with open(sentinel, "wb") as handle:
            handle.write(b"recoverable data")
        registry = os.path.join(self.home, ".copilot-sessions")
        with open(registry, "w", encoding="utf-8") as handle:
            handle.write("existing|Preserve me|timestamp|/worktree\n")
        for copy_mode, custom in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(copy=copy_mode, custom_prefix=custom):
                prefix = os.path.join(self.home, "custom", "bin") if custom else os.path.join(
                    self.home, ".local", "bin"
                )
                arguments = ["--copy"] if copy_mode else []
                if custom:
                    arguments += ["--prefix", prefix]
                result = run([INSTALL_SH, *arguments], env=self.env())
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = os.path.join(prefix, "copilot-session-cleanup.py")
                launcher = os.path.join(prefix, "copilot-s")
                self.assertEqual(os.path.islink(installed), not copy_mode)
                self.assertTrue(os.access(installed, os.X_OK))
                resolved = self._resolved_helper_path(
                    launcher, variable="SESSION_CLEANUP_HELPER"
                )
                expected = installed if copy_mode else CLEANUP_HELPER
                self.assertEqual(os.path.realpath(resolved), os.path.realpath(expected))
                override = os.path.join(self.home, "override-cleanup.py")
                self.assertEqual(
                    self._resolved_helper_path(
                        launcher, {"COPILOT_SESSION_CLEANUP_HELPER": override},
                        variable="SESSION_CLEANUP_HELPER",
                    ), override,
                )
                smoke = run([sys.executable, installed, "list"], env=self.env())
                self.assertEqual(smoke.returncode, 0, smoke.stdout + smoke.stderr)
                manifest = os.path.join(self.home, ".copilot-cli-toolkit", "install-manifest.txt")
                with open(manifest, encoding="utf-8") as handle:
                    self.assertIn(installed, handle.read().splitlines())
                removed = run([UNINSTALL_SH], env=self.env())
                self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
                self.assertFalse(os.path.lexists(installed))
                with open(sentinel, "rb") as handle:
                    self.assertEqual(handle.read(), b"recoverable data")
                with open(registry, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), "existing|Preserve me|timestamp|/worktree\n")


class TestMissingHelperIsVisibleNotSilent(BaseHomeTestCase):
    """A missing task report helper must never silently disable reporting:
    it must warn (session-exit/marker paths) or clearly error (--report)."""

    def _funcs_only_script(self):
        # bin/copilot-s ends with a bare `main "$@"` call; strip that so the
        # functions can be sourced and exercised directly without running
        # the interactive session manager.
        with open(COPILOT_S, encoding="utf-8") as f:
            lines = f.readlines()
        funcs_path = os.path.join(self.home, "copilot-s-funcs.sh")
        with open(funcs_path, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])
        return funcs_path

    def test_report_flag_gives_clear_error_for_missing_helper(self):
        # Use a standalone copy of copilot-s with no sibling helper and no
        # ~/.local/bin fallback present, so helper resolution genuinely
        # fails (running the real repo script directly would always find
        # the real helper next to it in bin/).
        standalone_dir = os.path.join(self.home, "standalone")
        os.makedirs(standalone_dir)
        standalone_copilot_s = os.path.join(standalone_dir, "copilot-s")
        shutil.copyfile(COPILOT_S, standalone_copilot_s)
        os.chmod(standalone_copilot_s, os.stat(standalone_copilot_s).st_mode | stat.S_IEXEC)

        result = run(
            [standalone_copilot_s, "--report", "ABC-123"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr)
        self.assertIn("copilot-task-report.py", result.stderr)

    def test_ensure_marker_warns_instead_of_silently_returning(self):
        funcs_path = self._funcs_only_script()
        script = (
            f'source "{funcs_path}"; '
            'TASK_REPORT_HELPER="/nonexistent/copilot-task-report.py"; '
            "ensure_task_install_marker"
        )
        result = run(["bash", "-c", script], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0)  # never fails hard
        self.assertIn("Warning", result.stderr)
        self.assertIn("not found", result.stderr)

    def test_update_task_report_warns_instead_of_silently_returning(self):
        funcs_path = self._funcs_only_script()
        script = (
            f'source "{funcs_path}"; '
            'TASK_REPORT_HELPER="/nonexistent/copilot-task-report.py"; '
            'update_task_report "sess-1" "ABC-1"'
        )
        result = run(["bash", "-c", script], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0)  # never fails hard
        self.assertIn("Warning", result.stderr)
        self.assertIn("not found", result.stderr)


class TestUpdateTaskReportSurfacesWarningsOnSuccess(BaseHomeTestCase):
    """update_task_report() must not silently discard stderr just because
    the underlying ingest call succeeded (exit 0) — e.g. a legacy-
    migration warning printed alongside a normal successful ingest must
    still reach the user, while a genuinely clean/quiet success must
    print nothing extra (no noise on the common path)."""

    def _funcs_only_script(self):
        with open(COPILOT_S, encoding="utf-8") as f:
            lines = f.readlines()
        funcs_path = os.path.join(self.home, "copilot-s-funcs.sh")
        with open(funcs_path, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])
        return funcs_path

    def _fake_helper(self, body):
        path = os.path.join(self.home, "fake-helper.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env python3\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def test_stderr_is_surfaced_when_ingest_succeeds_but_warns(self):
        helper = self._fake_helper(
            "import sys\n"
            "sys.stderr.write('warning: legacy migration found a conflict, both copies kept\\n')\n"
            "sys.exit(0)\n"
        )
        funcs_path = self._funcs_only_script()
        script = (
            f'source "{funcs_path}"; '
            f'TASK_REPORT_HELPER="{helper}"; '
            'update_task_report "sess-1" "ABC-1"'
        )
        result = run(["bash", "-c", script], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0)
        self.assertIn("legacy migration found a conflict", result.stderr)

    def test_stderr_stays_quiet_when_ingest_succeeds_cleanly(self):
        helper = self._fake_helper("import sys\nsys.exit(0)\n")
        funcs_path = self._funcs_only_script()
        script = (
            f'source "{funcs_path}"; '
            f'TASK_REPORT_HELPER="{helper}"; '
            'update_task_report "sess-1" "ABC-1"'
        )
        result = run(["bash", "-c", script], env=self.env(), stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


class TestSessionNameEventsPathIsSafe(BaseHomeTestCase):
    """get_session_name must pass the events.jsonl path as argv, not
    interpolate it into Python source, so paths with quotes/`$()`/spaces
    can never break out of the string literal or inject code."""

    def _funcs_only_script(self):
        with open(COPILOT_S, encoding="utf-8") as f:
            lines = f.readlines()
        funcs_path = os.path.join(self.home, "copilot-s-funcs.sh")
        with open(funcs_path, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])
        return funcs_path

    def test_get_session_name_handles_adversarial_path(self):
        funcs_path = self._funcs_only_script()
        weird_dir = os.path.join(self.home, "state", "it's_weird_$(touch pwned)")
        os.makedirs(weird_dir)
        with open(os.path.join(weird_dir, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"user.message","data":{"content":"Hello world"}}\n')

        script = f'source "{funcs_path}"; get_session_name "$1"'
        result = run(
            ["bash", "-c", script, "_", weird_dir],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Hello world")
        # The path must never have been executed as code.
        self.assertFalse(os.path.exists(os.path.join(self.home, "pwned")))
        self.assertFalse(os.path.exists("pwned"))


class TestInstallerHelpNotTruncated(BaseHomeTestCase):
    """install.sh/uninstall.sh --help must print the *entire* header
    comment block, not a brittle fixed line range that silently truncates
    when the header grows."""

    def _header_comment_lines(self, script_path):
        lines = []
        with open(script_path, encoding="utf-8") as f:
            all_lines = f.readlines()
        for line in all_lines[1:]:  # skip shebang
            if line.startswith("#"):
                lines.append(line)
            else:
                break
        return lines

    def test_install_help_prints_full_header(self):
        result = run([INSTALL_SH, "--help"], env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        header_lines = self._header_comment_lines(INSTALL_SH)
        # Every non-trivial line of the header comment must appear in the
        # rendered help output (this catches any fixed-range truncation).
        for line in header_lines:
            text = line.lstrip("#").strip()
            if text:
                self.assertIn(text, result.stdout, f"missing header line: {text!r}")

    def test_uninstall_help_prints_full_header(self):
        result = run([UNINSTALL_SH, "--help"], env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        header_lines = self._header_comment_lines(UNINSTALL_SH)
        for line in header_lines:
            text = line.lstrip("#").strip()
            if text:
                self.assertIn(text, result.stdout, f"missing header line: {text!r}")


class TestInstallerPrefixArgValidation(BaseHomeTestCase):
    """`--prefix` with no following value must fail fast with a clear
    error/usage and a nonzero exit code — not an `unbound variable` crash
    (install.sh runs under `set -euo pipefail`)."""

    def test_prefix_without_value_errors_cleanly(self):
        result = run(
            [INSTALL_SH, "--prefix"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertIn("--prefix", result.stderr)

    def test_prefix_without_value_does_not_crash_with_set_u(self):
        # Specifically guard against the `$2: unbound variable` failure
        # mode from `set -u` when --prefix is the last argument.
        result = run(
            [INSTALL_SH, "--dry-run", "--prefix"],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unbound variable", result.stderr)

    def test_prefix_with_value_still_works(self):
        custom_prefix = os.path.join(self.home, "custom-bin")
        result = run(
            [INSTALL_SH, "--prefix", custom_prefix],
            env=self.env(),
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(custom_prefix, "copilot-s")))


class TestContextMetricsSessionManager(BaseHomeTestCase):
    """Drive copilot-s through a PTY with a fake Copilot CLI and helper to
    check native naming, conservative enrollment, and stop-time handling."""

    CONTEXT_SUMMARY = (
        "Context: 73.2% (732/1,000 tokens) last observed before compaction "
        "· Compactions: 2 successful"
    )

    def setUp(self):
        super().setUp()
        self.cwd = os.path.realpath(os.path.join(self.home, "worktree"))
        os.makedirs(self.cwd)
        subprocess.run(["git", "init", "-q", self.cwd], check=True)
        subprocess.run(
            ["git", "-C", self.cwd, "symbolic-ref", "HEAD", "refs/heads/ABC-123"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.cwd, "-c", "user.name=Tester", "-c",
             "user.email=test@example.com", "commit", "--allow-empty", "-m", "fixture"],
            check=True, stdout=subprocess.DEVNULL,
        )
        self.state_dir = os.path.join(self.home, ".copilot", "session-state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.sessions_file = os.path.join(self.home, ".copilot-sessions")
        self.fake_bin = os.path.join(self.home, "fake-bin")
        os.makedirs(self.fake_bin)
        self.helper_log = os.path.join(self.home, "helper.jsonl")
        self.copilot_log = os.path.join(self.home, "copilot.json")
        self.helper = os.path.join(self.home, "fake-task-report.py")
        with open(self.helper, "w", encoding="utf-8") as f:
            f.write(
                "#!%s\n"
                "import json, os, sys\n"
                "with open(os.environ['HELPER_LOG'], 'a', encoding='utf-8') as log:\n"
                "    log.write(json.dumps({'command': sys.argv[1], 'args': sys.argv[2:]}) + '\\n')\n"
                "command = sys.argv[1]\n"
                "if command == 'context-list':\n"
                "    labels = json.loads(os.environ.get('FAKE_CONTEXT_LABELS', '{}'))\n"
                "    for session_id in sys.stdin.read().splitlines():\n"
                "        print(labels.get(session_id, ''))\n"
                "elif command == 'context-update':\n"
                "    if os.environ.get('FAIL_CONTEXT_UPDATE') == '1':\n"
                "        print('simulated context update failure', file=sys.stderr)\n"
                "        sys.exit(7)\n"
                "    if os.environ.get('CONTEXT_UPDATE_EXIT'):\n"
                "        print('simulated context enrollment verification warning', file=sys.stderr)\n"
                "        sys.exit(int(os.environ['CONTEXT_UPDATE_EXIT']))\n"
                "    print(%r)\n"
                % (sys.executable, self.CONTEXT_SUMMARY)
            )
        os.chmod(self.helper, os.stat(self.helper).st_mode | stat.S_IEXEC)

        copilot = os.path.join(self.fake_bin, "copilot")
        with open(copilot, "w", encoding="utf-8") as f:
            f.write(
                "#!%s\n"
                "import datetime, json, os, pathlib, sys\n"
                "with open(os.environ['COPILOT_LOG'], 'w', encoding='utf-8') as log:\n"
                "    json.dump(sys.argv[1:], log)\n"
                "def option_value(long_flag, short_flag=None):\n"
                "    args = iter(sys.argv[1:])\n"
                "    for arg in args:\n"
                "        if arg == '--':\n"
                "            break\n"
                "        if arg == long_flag or (short_flag and arg == short_flag):\n"
                "            value = next(args, None)\n"
                "            if value is not None and not value.startswith('-'):\n"
                "                return value\n"
                "            if value == '--':\n"
                "                break\n"
                "        elif arg.startswith(long_flag + '='):\n"
                "            return arg.split('=', 1)[1]\n"
                "        elif short_flag and arg.startswith(short_flag) and arg != short_flag:\n"
                "            return arg[len(short_flag):]\n"
                "        # Only the required-value options exercised by these fixtures.\n"
                "        elif arg in ('-p', '--prompt', '-i', '--interactive', '--agent',\n"
                "                     '--model', '--auto-tier', '-n', '--name', '--session-id'):\n"
                "            if next(args, None) == '--':\n"
                "                break\n"
                "    return None\n"
                "name = option_value('--name', '-n')\n"
                "root = pathlib.Path(os.environ['HOME']) / '.copilot' / 'session-state'\n"
                "mode = os.environ.get('FAKE_COPILOT_MODE', 'idle')\n"
                "launched_directory = None\n"
                "if mode in ('new', 'multiple'):\n"
                "    session_ids = os.environ.get('FAKE_SESSION_IDS', 'fresh-session').split(',')\n"
                "    for index, session_id in enumerate(session_ids):\n"
                "        directory = root / session_id\n"
                "        directory.mkdir(parents=True, exist_ok=True)\n"
                "        created = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')\n"
                "        cwd = os.environ.get('FAKE_SESSION_CWD', os.getcwd())\n"
                "        title = (name if name is not None else 'Fake session') if index == 0 else 'Concurrent session'\n"
                "        (directory / 'workspace.yaml').write_text(\n"
                "            'created_at: ' + created + '\\n' + 'cwd: ' + cwd + '\\n' + 'name: ' + title + '\\n',\n"
                "            encoding='utf-8')\n"
                "        if index == 0:\n"
                "            launched_directory = directory\n"
                "else:\n"
                "    session_id = (os.environ.get('FAKE_RESUMED_SESSION') or\n"
                "                  option_value('--resume', '-r') or option_value('--session-id'))\n"
                "    if session_id:\n"
                "        launched_directory = root / session_id\n"
                "def set_title(directory, title):\n"
                "    workspace = directory / 'workspace.yaml'\n"
                "    lines = [line for line in workspace.read_text(encoding='utf-8').splitlines()\n"
                "             if not line.startswith('name:')]\n"
                "    workspace.write_text('\\n'.join(lines) + '\\nname: ' + title + '\\n', encoding='utf-8')\n"
                "if launched_directory is not None:\n"
                "    if name is not None:\n"
                "        set_title(launched_directory, name)\n"
                "    title = next(line[6:] for line in (launched_directory / 'workspace.yaml').read_text(\n"
                "        encoding='utf-8').splitlines() if line.startswith('name: '))\n"
                "    print('FAKE_COPILOT_TITLE: ' + json.dumps(title), flush=True)\n"
                "    if 'FAKE_RENAME_TO' in os.environ:\n"
                "        set_title(launched_directory, os.environ['FAKE_RENAME_TO'])\n"
                % sys.executable
            )
        os.chmod(copilot, os.stat(copilot).st_mode | stat.S_IEXEC)

    def _env(self, extra=None):
        additions = {
            "PATH": os.pathsep.join(
                [self.fake_bin, os.path.dirname(sys.executable), "/usr/bin", "/bin"]
            ),
            "COPILOT_TASK_REPORT_HELPER": self.helper,
            "HELPER_LOG": self.helper_log,
            "COPILOT_LOG": self.copilot_log,
            "PWD": self.cwd,
        }
        if extra:
            additions.update(extra)
        return self.env(additions)

    def _run_tty(
        self, env, actions=(), timeout=20, copilot_s_args=(), input_burst=None
    ):
        """Run the real script with a controlling terminal and answer prompts
        only when they appear, so flush_stdin cannot consume later answers.

        ``input_burst`` is a (prompt marker, text) pair used to simulate
        multiple lines pasted together while the first prompt is displayed.
        An action response may be a callable returning text, for changes
        synchronized with a real prompt (such as acquiring a test flock).
        """
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.cwd)
            os.execve(
                "/bin/bash",
                ["/bin/bash", COPILOT_S, *copilot_s_args],
                env,
            )
        output = bytearray()
        actions = list(actions)
        action_index = 0
        input_burst_sent = False
        status = None
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        if status is None:
                            try:
                                _, child_status = os.waitpid(pid, 0)
                                status = os.waitstatus_to_exitcode(child_status)
                            except ChildProcessError:
                                pass
                        break
                    if not chunk:
                        if status is None:
                            try:
                                _, child_status = os.waitpid(pid, 0)
                                status = os.waitstatus_to_exitcode(child_status)
                            except ChildProcessError:
                                pass
                        break
                    output.extend(chunk)
                    if (
                        input_burst is not None
                        and not input_burst_sent
                        and input_burst[0].encode() in output
                    ):
                        os.write(master, input_burst[1].encode())
                        input_burst_sent = True
                    while action_index < len(actions):
                        marker, response = actions[action_index]
                        if marker.encode() not in output:
                            break
                        if callable(response):
                            response = response()
                        os.write(master, response.encode())
                        action_index += 1
                if status is None:
                    waited, child_status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        status = os.waitstatus_to_exitcode(child_status)
                elif not readable:
                    break
            else:
                raise AssertionError(
                    "copilot-s PTY run timed out:\n%s"
                    % output.decode("utf-8", errors="replace")
                )
        finally:
            if status is None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            os.close(master)
        return (
            status,
            output.decode("utf-8", errors="replace"),
            action_index + int(input_burst_sent),
        )

    def _helper_calls(self):
        if not os.path.exists(self.helper_log):
            return []
        with open(self.helper_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _session(self, session_id, *, sidecar=False, name=None, cwd=None):
        directory = os.path.join(self.state_dir, session_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "workspace.yaml"), "w", encoding="utf-8") as f:
            f.write(
                "created_at: 2026-10-13T10:00:00Z\n"
                "cwd: %s\n"
                "branch: ABC-123\n"
                "name: %s\n"
                % (cwd or self.cwd, name or session_id)
            )
        if sidecar:
            with open(os.path.join(directory, "copilot-s-context.json"), "w", encoding="utf-8") as f:
                f.write("{}\n")
        return directory

    def _commands(self, name):
        return [call for call in self._helper_calls() if call["command"] == name]

    def _copilot_args(self):
        with open(self.copilot_log, encoding="utf-8") as f:
            return json.load(f)

    def _native_name(self, session_id):
        with open(
            os.path.join(self.state_dir, session_id, "workspace.yaml"),
            encoding="utf-8",
        ) as f:
            return next(line[6:].rstrip("\n") for line in f if line.startswith("name: "))

    def _session_rows(self):
        with open(self.sessions_file, encoding="utf-8") as f:
            return [line.rstrip("\n").split("|") for line in f if line.strip()]

    def _cleanup_fixture(self, number, cwd=None):
        session_id = "12345678-1234-1234-abcd-%012x" % number
        directory = os.path.join(self.state_dir, session_id)
        os.makedirs(directory)
        with open(os.path.join(directory, "workspace.yaml"), "w", encoding="utf-8") as handle:
            handle.write(
                "id: %s\ncwd: %s\nuser_named: false\n"
                "created_at: 2020-01-01T00:00:00Z\nupdated_at: 2020-01-01T00:00:00Z\n"
                % (session_id, cwd or self.cwd)
            )
        os.utime(os.path.join(directory, "workspace.yaml"), (1577836800, 1577836800))
        os.utime(directory, (1577836800, 1577836800))
        return session_id, directory

    def _cleanup_env(self):
        # The launcher invokes the real helper through a test-only import
        # wrapper patching time, never eligibility or the activity probe.
        wrapper = os.path.join(self.home, "cleanup-clock-fixture.py")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                "import importlib.util, sys, time\n"
                "from unittest import mock\n"
                "spec = importlib.util.spec_from_file_location('cleanup', %r)\n"
                "cleanup = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(cleanup)\n"
                "with mock.patch.object(cleanup, 'now_epoch', return_value=time.time() + 172800):\n"
                "    sys.exit(cleanup.main())\n" % CLEANUP_HELPER
            )
        lsof = os.path.join(self.fake_bin, "lsof")
        with open(lsof, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(lsof, 0o755)
        return self._env({"COPILOT_SESSION_CLEANUP_HELPER": wrapper})

    def _cleanup_recent_fixture(self, number):
        session_id, directory = self._cleanup_fixture(number)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        with open(os.path.join(directory, "workspace.yaml"), "w", encoding="utf-8") as handle:
            handle.write(
                "id: %s\ncwd: %s\nuser_named: false\ncreated_at: %s\nupdated_at: %s\n"
                % (session_id, self.cwd, stamp, stamp)
            )
        return session_id, directory

    def _cleanup_recording_env(self):
        # Log argv, but run the real helper with its real clock, stat and rename.
        wrapper = os.path.join(self.home, "cleanup-recording-fixture.py")
        log_path = os.path.join(self.home, "cleanup-options.jsonl")
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("")
        with open(wrapper, "w", encoding="utf-8") as handle:
            handle.write(
                "import json, os, subprocess, sys\n"
                "with open(os.environ['CLEANUP_LOG'], 'a', encoding='utf-8') as log:\n"
                "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "sys.exit(subprocess.run([sys.executable, %r, *sys.argv[1:]]).returncode)\n"
                % CLEANUP_HELPER
            )
        lsof = os.path.join(self.fake_bin, "lsof")
        with open(lsof, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(lsof, 0o755)
        return self._env({
            "COPILOT_SESSION_CLEANUP_HELPER": wrapper, "CLEANUP_LOG": log_path,
        })

    def _cleanup_calls(self):
        with open(os.path.join(self.home, "cleanup-options.jsonl"), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_cleanup_current_scope_leaves_foreign_and_named_sessions_usable(self):
        local, local_dir = self._cleanup_fixture(1)
        foreign, foreign_dir = self._cleanup_fixture(2, cwd=os.path.join(self.home, "other"))
        kept_dir = self._session("named-kept", name="Keep this named session")
        status, output, actions = self._run_tty(
            self._cleanup_env(),
            actions=[
                ("Select session to resume", "c\n"),
                ("Also include recent empty sessions", "\n"),
                ("Type 'yes' to move 1 session(s)", "yes\n"),
                ("Moved: 1", "q\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 4, output)
        self.assertFalse(os.path.exists(local_dir), output)
        self.assertTrue(os.path.isdir(foreign_dir))
        self.assertTrue(os.path.isdir(kept_dir))
        self.assertNotIn(local, [row[0] for row in self._session_rows()])
        self.assertIn(foreign, [row[0] for row in self._session_rows()])
        self.assertFalse(os.path.exists(self.copilot_log), output)
        self.assertIn("Bye!", output)

    def test_cleanup_all_final_empty_list_never_auto_launches_copilot(self):
        _, first_dir = self._cleanup_fixture(1)
        _, second_dir = self._cleanup_fixture(2, cwd=os.path.join(self.home, "other"))
        status, output, actions = self._run_tty(
            self._cleanup_env(), copilot_s_args=["--all"],
            actions=[
                ("Select session to resume", "c\n"),
                ("Also include recent empty sessions", "\n"),
                ("Type 'yes' to move 2 session(s)", "yes\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 3, output)
        self.assertIn("Moved: 2", output)
        self.assertIn("No sessions left. Run copilot-s again", output)
        self.assertFalse(os.path.exists(first_dir))
        self.assertFalse(os.path.exists(second_dir))
        self.assertFalse(os.path.exists(self.copilot_log), output)
        quarantine = os.path.join(self.home, ".copilot-s-quarantine")
        self.assertEqual(len(os.listdir(quarantine)), 1)

    def test_cleanup_confirmation_cancels_padded_yes_enter_and_terminal_eof(self):
        _, directory = self._cleanup_fixture(1)
        environment = self._cleanup_env()
        for answer in ("Yes\n", " yes\n", "yes \n", "\n", "\x04"):
            with self.subTest(answer=answer):
                status, output, actions = self._run_tty(
                    environment,
                    actions=[
                        ("Select session to resume", "c\n"),
                        ("Also include recent empty sessions", "\n"),
                        ("Type 'yes' to move 1 session(s)", answer),
                        ("Cancelled. Nothing was changed.", "q\n"),
                    ],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 4, output)
                self.assertTrue(os.path.isdir(directory))
                self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot-s-quarantine")))
                self.assertFalse(os.path.exists(self.copilot_log))

    def test_missing_cleanup_helper_and_preview_error_leave_normal_menu_usable(self):
        self._session("cleanup-visible", name="Still usable")
        failing = os.path.join(self.home, "failing-cleanup.py")
        with open(failing, "w", encoding="utf-8") as handle:
            handle.write("import sys\nprint('fixture preview error', file=sys.stderr)\nsys.exit(7)\n")
        for helper, marker in (
            (os.path.join(self.home, "missing.py"), "Session cleanup helper not found"),
            (failing, "Cleanup preview failed (exit 7)"),
        ):
            with self.subTest(helper=helper):
                responses = [("Select session to resume", "c\n")]
                if helper == failing:
                    responses.append(("Also include recent empty sessions", "\n"))
                responses.append((marker, "q\n"))
                status, output, actions = self._run_tty(
                    self._env({"COPILOT_SESSION_CLEANUP_HELPER": helper}),
                    actions=responses,
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, len(responses), output)
                self.assertIn("Still usable", output)
                self.assertIn("Bye!", output)
                self.assertFalse(os.path.exists(self.copilot_log))

    def test_cleanup_recent_optin_spellings_use_same_policy_for_preview_and_apply(self):
        for number, answer in enumerate(("y\n", "Y\n", "yes\n", "Yes\n", "YES\n"), 1):
            with self.subTest(answer=answer):
                sid, directory = self._cleanup_recent_fixture(number)
                environment = self._cleanup_recording_env()
                all_scope = number % 2 == 0
                def confirm_move():
                    self.assertTrue(os.path.isdir(directory), "opt-in alone must not move")
                    self.assertEqual(len(self._cleanup_calls()), 1, "only preview before exact yes")
                    return "yes\n"
                status, output, actions = self._run_tty(
                    environment, copilot_s_args=["--all"] if all_scope else [],
                    actions=[
                        ("Select session to resume", "c\n"),
                        ("Also include recent empty sessions", answer),
                        ("Type 'yes' to move 1 session(s)", confirm_move),
                    ],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 3, output)
                self.assertIn("Recent sessions included. Warning:", output)
                self.assertIn("Moved: 1   Skipped: 0   Failed: 0", output)
                self.assertFalse(os.path.exists(directory), output)
                calls = self._cleanup_calls()
                self.assertEqual(len(calls), 2)
                self.assertIn("preview", calls[0])
                self.assertIn("apply", calls[1])
                for call in calls:
                    self.assertEqual(call.count("--include-recent"), 1)
                    if all_scope:
                        self.assertIn("--all", call)
                        self.assertNotIn("--cwd", call)
                    else:
                        self.assertEqual(call[call.index("--cwd") + 1], self.cwd)
                self.assertNotIn(sid, [row[0] for row in self._session_rows()])
                self.assertFalse(os.path.exists(self.copilot_log), output)

    def test_cleanup_recent_default_enter_no_eof_and_other_answers_keep_recent(self):
        _, directory = self._cleanup_recent_fixture(1)
        for answer in ("\n", "n\n", "\x04", "No\n", "yEs\n", " yes\n", "yes \n", "other\n"):
            with self.subTest(answer=answer):
                status, output, actions = self._run_tty(
                    self._cleanup_recording_env(),
                    actions=[
                        ("Select session to resume", "c\n"),
                        ("Also include recent empty sessions", answer),
                        ("No empty sessions are eligible for quarantine.", "q\n"),
                    ],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 3, output)
                self.assertIn("[y/N]", output)
                self.assertIn("within the last 24h", output)
                self.assertNotIn("Recent sessions included. Warning:", output)
                self.assertNotIn("Type 'yes' to move", output)
                calls = self._cleanup_calls()
                self.assertEqual(len(calls), 1)
                self.assertIn("preview", calls[0])
                self.assertNotIn("--include-recent", calls[0])
                self.assertTrue(os.path.isdir(directory))
                self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot-s-quarantine")))
                self.assertFalse(os.path.exists(self.copilot_log))

    def test_cleanup_recent_optin_still_requires_separate_exact_yes_confirmation(self):
        _, directory = self._cleanup_recent_fixture(1)
        for answer in ("Yes\n", " yes\n", "yes \n", "\n", "\x04"):
            with self.subTest(answer=answer):
                status, output, actions = self._run_tty(
                    self._cleanup_recording_env(),
                    actions=[
                        ("Select session to resume", "c\n"),
                        ("Also include recent empty sessions", "y\n"),
                        ("Type 'yes' to move 1 session(s)", answer),
                        ("Cancelled. Nothing was changed.", "q\n"),
                    ],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 4, output)
                self.assertTrue(os.path.isdir(directory))
                calls = self._cleanup_calls()
                self.assertEqual(len(calls), 1, "cancellation must never invoke apply")
                self.assertIn("--include-recent", calls[0])
                self.assertFalse(os.path.exists(os.path.join(self.home, ".copilot-s-quarantine")))
                self.assertFalse(os.path.exists(self.copilot_log))

    def test_fresh_session_enrolls_exact_id_and_prints_metrics_before_keep_prompt(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "fresh-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertLess(output.index(self.CONTEXT_SUMMARY), output.index("[K/r/d]:"))
        calls = self._commands("context-update")
        self.assertEqual(len(calls), 1)
        args = calls[0]["args"]
        self.assertIn("--session-id", args)
        self.assertEqual(args[args.index("--session-id") + 1], "fresh-session")
        self.assertIn("--enroll", args)
        self.assertEqual(args[args.index("--expect-cwd") + 1], self.cwd)
        started_after = float(args[args.index("--started-after") + 1])
        self.assertLessEqual(started_after, time.time())
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "other-session",
                                                     "copilot-s-context.json")))
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "fresh-session")
        self.assertEqual(len(rows[0]), 4)  # preserve the legacy index format

    def test_new_launch_forces_auto_and_preserves_non_model_arguments(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "new-auto-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "\n"),
                ("[K/r/d]:", "k\n"),
            ],
            copilot_s_args=[
                "--model", "caller-model",
                "--auto-tier", "caller-tier",
                "--custom-option", "kept-value",
                "--model=other-caller-model",
                "--auto-tier=other-caller-tier",
                "--another-option=value",
                "positional-argument",
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        with open(self.copilot_log, encoding="utf-8") as f:
            copilot_args = json.load(f)
        self.assertEqual(
            copilot_args,
            [
                "--custom-option", "kept-value",
                "--another-option=value",
                "positional-argument",
                "--model", "auto",
                "--auto-tier", "balance",
            ],
        )

    def test_new_session_passes_prompted_name_at_launch_and_persists_native_title(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "named-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "Planning notes\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertLess(
            output.index("Session name (Enter for automatic):"),
            output.index("Starting copilot..."),
            "the name must be requested before Copilot launches",
        )
        copilot_args = self._copilot_args()
        self.assertEqual(
            copilot_args,
            ["--model", "auto", "--auto-tier", "balance", "--name=Planning notes"],
        )
        self.assertIn('FAKE_COPILOT_TITLE: "Planning notes"', output)
        self.assertEqual(self._native_name("named-session"), "Planning notes")
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        named_rows = [row for row in rows if row[0] == "named-session"]
        self.assertEqual(len(named_rows), 1)
        self.assertEqual(named_rows[0][1], "Planning notes")

    def test_dash_leading_prompted_name_is_one_native_name_argument(self):
        title = "-n native --continue --model"
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "dash-named-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", title + "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertEqual(
            self._copilot_args(),
            ["--model", "auto", "--auto-tier", "balance", "--name=" + title],
        )
        self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(title), output)
        self.assertEqual(self._native_name("dash-named-session"), title)
        self.assertEqual(self._session_rows()[0][1], title)

    def test_native_value_operands_are_not_names_selectors_or_model_options(self):
        cases = [
            ["-p", "-n foo"],
            ["--prompt", "--continue"],
            ["-i", "--model"],
        ]
        title = "Prompted native title"
        for index, caller_args in enumerate(cases):
            with self.subTest(args=caller_args):
                session_id = "native-operand-%s" % index
                actions = [
                    ("Session name (Enter for automatic):", title + "\n"),
                    ("[K/r/d]:", "k\n"),
                ]
                if index:
                    actions.insert(0, ("Select session to resume", "n\n"))
                status, output, answered = self._run_tty(
                    self._env({
                        "FAKE_COPILOT_MODE": "new",
                        "FAKE_SESSION_IDS": session_id,
                    }),
                    actions=actions,
                    copilot_s_args=caller_args,
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(answered, len(actions), output)
                self.assertEqual(
                    self._copilot_args(),
                    caller_args + [
                        "--model", "auto", "--auto-tier", "balance", "--name=" + title,
                    ],
                )
                self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(title), output)
                self.assertEqual(self._native_name(session_id), title)

    def test_wrapper_flags_as_native_operands_do_not_trigger_wrapper_actions(self):
        self._session("local-session", name="Visible local session")
        self._session(
            "foreign-session",
            name="Foreign session must remain hidden",
            cwd=os.path.join(self.home, "another-worktree"),
        )
        cases = [
            ["-p", "--report"],
            ["--prompt", "--task=ABC-999"],
            ["-i", "--all"],
            ["--interactive", "-a"],
            ["--agent", "--help"],
            ["--agent", "--version"],
        ]
        title = "Operand-safe title"
        for index, caller_args in enumerate(cases):
            with self.subTest(args=caller_args):
                session_id = "wrapper-operand-%s" % index
                status, output, actions = self._run_tty(
                    self._env({
                        "FAKE_COPILOT_MODE": "new",
                        "FAKE_SESSION_IDS": session_id,
                    }),
                    actions=[
                        ("Select session to resume", "n\n"),
                        ("Session name (Enter for automatic):", title + "\n"),
                        ("[K/r/d]:", "k\n"),
                    ],
                    copilot_s_args=caller_args,
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 3, output)
                self.assertIn("Visible local session", output)
                self.assertNotIn("Foreign session must remain hidden", output)
                self.assertNotIn("Usage:", output)
                self.assertEqual(self._commands("report"), [])
                self.assertEqual(
                    self._copilot_args(),
                    caller_args + [
                        "--model", "auto", "--auto-tier", "balance", "--name=" + title,
                    ],
                )
                self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(title), output)
                self.assertEqual(self._native_name(session_id), title)

    def test_double_dash_remainder_is_exact_and_forced_options_precede_it(self):
        self._session("local-session", name="Visible local session")
        self._session(
            "foreign-session",
            name="Foreign session must remain hidden",
            cwd=os.path.join(self.home, "another-worktree"),
        )
        remainder = [
            "--",
            "--name", "Remainder title", "--name=Other remainder title", "-nattached",
            "--resume", "not-a-session", "-rattached", "--continue",
            "--session-id=remainder-id", "--connect=unreachable",
            "--model", "remainder-model", "--auto-tier=remainder-tier",
            "--report", "ABC-999", "--task=ABC-999",
            "--all", "-a", "--help", "-h", "--version", "-v",
            "", "  spaced remainder  ", "first line\nsecond line", "--",
        ]
        for index, title in enumerate(("Before-boundary title", "")):
            with self.subTest(prompted_title=title):
                session_id = "boundary-session-%s" % index
                status, output, actions = self._run_tty(
                    self._env({
                        "FAKE_COPILOT_MODE": "new",
                        "FAKE_SESSION_IDS": session_id,
                    }),
                    actions=[
                        ("Select session to resume", "n\n"),
                        ("Session name (Enter for automatic):", title + "\n"),
                        ("[K/r/d]:", "k\n"),
                    ],
                    copilot_s_args=[
                        "--model", "caller-model", "--auto-tier=caller-tier",
                        "--prompt", "Prompt before boundary", *remainder,
                    ],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 3, output)
                expected_prefix = [
                    "--prompt", "Prompt before boundary",
                    "--model", "auto", "--auto-tier", "balance",
                ]
                if title:
                    expected_prefix.append("--name=" + title)
                self.assertEqual(self._copilot_args(), expected_prefix + remainder)
                self.assertNotIn("Foreign session must remain hidden", output)
                self.assertNotIn("Usage:", output)
                self.assertEqual(self._commands("report"), [])
                native_title = title or "Fake session"
                self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(native_title), output)
                self.assertEqual(self._native_name(session_id), native_title)
                row = next(row for row in self._session_rows() if row[0] == session_id)
                self.assertEqual(row[1], native_title)

    def test_missing_native_operand_does_not_consume_double_dash(self):
        # Assert forwarding only. The native CLI, not this fake or wrapper,
        # is responsible for rejecting these incomplete required options.
        remainder = ["--", "--report", "ABC-999", "--name=Positional title", ""]
        for option in ("--prompt", "--name", "--session-id"):
            with self.subTest(option=option):
                actions = (
                    [("Session name (Enter for automatic):", "\n")]
                    if option == "--prompt" else []
                )
                status, output, answered = self._run_tty(
                    self._env({"FAKE_COPILOT_MODE": "idle"}),
                    actions=actions,
                    copilot_s_args=[option, *remainder],
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(answered, len(actions), output)
                self.assertEqual(
                    self._copilot_args(),
                    [option, "--model", "auto", "--auto-tier", "balance"] + remainder,
                )
                self.assertEqual(self._commands("report"), [])

    def test_blank_new_session_name_uses_automatic_name(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "automatic-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertEqual(
            self._copilot_args(), ["--model", "auto", "--auto-tier", "balance"]
        )
        self.assertIn('FAKE_COPILOT_TITLE: "Fake session"', output)
        self.assertEqual(self._native_name("automatic-session"), "Fake session")
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        automatic_rows = [row for row in rows if row[0] == "automatic-session"]
        self.assertEqual(len(automatic_rows), 1)
        self.assertEqual(automatic_rows[0][1], "Fake session")

    def test_whitespace_only_new_session_name_uses_automatic_name(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "whitespace-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "   \t  \n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertEqual(
            self._copilot_args(), ["--model", "auto", "--auto-tier", "balance"]
        )
        self.assertEqual(self._native_name("whitespace-session"), "Fake session")
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        whitespace_rows = [row for row in rows if row[0] == "whitespace-session"]
        self.assertEqual(len(whitespace_rows), 1)
        self.assertEqual(whitespace_rows[0][1], "Fake session")

    def test_concurrent_session_cannot_receive_another_launches_prompted_name(self):
        chosen_name = "Owned launch title"
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "multiple",
                # The permissive post-session detector finds the unrelated
                # directory first. Native naming must still affect only the
                # fake CLI's own session (the first ID in this list).
                "FAKE_SESSION_IDS": "z-owned-session,a-concurrent-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", chosen_name + "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        copilot_args = self._copilot_args()
        self.assertEqual(
            copilot_args,
            ["--model", "auto", "--auto-tier", "balance", "--name=" + chosen_name],
        )
        self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(chosen_name), output)
        self.assertEqual(self._native_name("z-owned-session"), chosen_name)
        self.assertEqual(self._native_name("a-concurrent-session"), "Concurrent session")
        concurrent_rows = [
            row for row in self._session_rows() if row[0] == "a-concurrent-session"
        ]
        self.assertEqual(len(concurrent_rows), 1)
        self.assertEqual(concurrent_rows[0][1], "Concurrent session")
        self.assertIn("could not unambiguously identify", output)
        self.assertEqual(self._commands("context-update"), [])

    def test_caller_name_spellings_skip_prompt_and_pass_through_unchanged(self):
        title = "  Caller|native title  "
        spellings = [
            ["--name", title],
            ["--name=" + title],
            ["-n", title],
            ["-n" + title],
        ]
        for index, name_args in enumerate(spellings):
            with self.subTest(args=name_args):
                session_id = "caller-named-%s" % index
                actions = [("[K/r/d]:", "k\n")]
                if index:
                    actions.insert(0, ("Select session to resume", "n\n"))
                caller_args = ["--custom-option", "kept", *name_args, "positional"]
                status, output, answered = self._run_tty(
                    self._env({
                        "FAKE_COPILOT_MODE": "new",
                        "FAKE_SESSION_IDS": session_id,
                    }),
                    actions=actions,
                    copilot_s_args=caller_args,
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(answered, len(actions), output)
                self.assertNotIn("Session name (Enter for automatic):", output)
                self.assertEqual(
                    self._copilot_args(),
                    caller_args + ["--model", "auto", "--auto-tier", "balance"],
                )
                self.assertIn("FAKE_COPILOT_TITLE: " + json.dumps(title), output)
                self.assertEqual(self._native_name(session_id), title)

    def test_caller_session_selectors_skip_prompt_without_clobbering_title(self):
        directory = self._session("selected-session", name="Existing native title")
        workspace = os.path.join(directory, "workspace.yaml")
        with open(workspace, "rb") as f:
            original_workspace = f.read()
        selectors = [
            ["--resume", "selected-session"],
            ["--resume=selected-session"],
            ["--resume"],
            ["-r", "selected-session"],
            ["-rselected-session"],
            ["-r"],
            ["--continue"],
            ["--session-id", "selected-session"],
            ["--session-id=selected-session"],
            ["--connect", "https://example.invalid/session"],
            ["--connect=https://example.invalid/session"],
        ]
        for selector in selectors:
            with self.subTest(args=selector):
                status, output, actions = self._run_tty(
                    self._env({
                        "FAKE_COPILOT_MODE": "idle",
                        "FAKE_RESUMED_SESSION": "selected-session",
                    }),
                    # Exercise caller selection with no wrapper-selected ID.
                    actions=[("Select session to resume", "n\n")],
                    copilot_s_args=selector,
                )
                self.assertEqual(status, 0, output)
                self.assertEqual(actions, 1, output)
                self.assertNotIn("Session name (Enter for automatic):", output)
                self.assertEqual(
                    self._copilot_args(),
                    selector + ["--model", "auto", "--auto-tier", "balance"],
                )
                self.assertIn('FAKE_COPILOT_TITLE: "Existing native title"', output)
                with open(workspace, "rb") as f:
                    self.assertEqual(f.read(), original_workspace)
                self.assertEqual(self._session_rows()[0][1], "Existing native title")

    def test_in_session_rename_is_not_replaced_by_prompted_name_after_exit(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "renamed-session",
                "FAKE_RENAME_TO": "Renamed during session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "Initial title\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertIn('FAKE_COPILOT_TITLE: "Initial title"', output)
        self.assertEqual(self._native_name("renamed-session"), "Renamed during session")
        self.assertEqual(self._session_rows()[0][1], "Renamed during session")
        self.assertIn("Session kept:", output)
        self.assertNotIn("Session kept:\x1b[0m Initial title", output)

    def test_exit_menu_rename_remains_wrapper_alias_only(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "aliased-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", "Native title\n"),
                ("[K/r/d]:", "r\n"),
                ("New name:", "Wrapper alias\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 3, output)
        self.assertEqual(self._native_name("aliased-session"), "Native title")
        self.assertEqual(self._session_rows()[0][1], "Wrapper alias")

    def _assert_post_session_save_is_honest_under_held_flock(self, rename):
        session_id = "busy-save-session"
        directory = self._session(session_id, name="Native title")
        original = "%s|Original wrapper alias|2026-10-13T10:00:00Z|%s\n" % (
            session_id, self.cwd,
        )
        with open(self.sessions_file, "w", encoding="utf-8") as handle:
            handle.write(original)
        lock_path = self.sessions_file + ".lock"
        with open(lock_path, "a", encoding="utf-8") as holder:
            def hold_at_prompt():
                # Initial listing must finish normally before contention.
                fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return "r\n" if rename else "k\n"
            actions = [
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", hold_at_prompt),
            ]
            if rename:
                actions.append(("New name:", "Unsaved new alias\n"))
            status, output, answered = self._run_tty(
                self._env({"FAKE_COPILOT_MODE": "idle"}), actions=actions,
            )
        self.assertEqual(status, 0, output)
        self.assertEqual(answered, len(actions), output)
        self.assertIn("NOT saved" if rename else "NOT updated", output)
        self.assertIn("session list is busy", output)
        self.assertNotIn("Session saved:", output)
        self.assertNotIn("Session kept:", output)
        with open(self.sessions_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)
        self.assertTrue(os.path.isdir(directory))
        self.assertEqual(self._native_name(session_id), "Native title")

    def test_exit_menu_rename_under_held_flock_does_not_claim_save(self):
        self._assert_post_session_save_is_honest_under_held_flock(rename=True)

    def test_exit_menu_keep_under_held_flock_does_not_claim_update(self):
        self._assert_post_session_save_is_honest_under_held_flock(rename=False)

    def test_pipe_in_new_session_name_does_not_corrupt_session_fields(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "pipe-session",
            }),
            actions=[
                ("Session name (Enter for automatic):", " \t Planning|notes \t \n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        copilot_args = self._copilot_args()
        self.assertEqual(
            copilot_args,
            ["--model", "auto", "--auto-tier", "balance", "--name=Planning notes"],
        )
        self.assertIn('FAKE_COPILOT_TITLE: "Planning notes"', output)
        self.assertEqual(self._native_name("pipe-session"), "Planning notes")
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        pipe_rows = [row for row in rows if row[0] == "pipe-session"]
        self.assertEqual(len(pipe_rows), 1)
        self.assertEqual(len(pipe_rows[0]), 4)
        self.assertEqual(pipe_rows[0][1], "Planning notes")
        self.assertEqual(pipe_rows[0][3], self.cwd)

    def test_task_id_prompt_still_accepts_input_for_a_new_session(self):
        subprocess.run(
            [
                "git", "-C", self.cwd, "symbolic-ref", "HEAD",
                "refs/heads/feature/ordinary-work",
            ],
            check=True,
        )
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "tasked-session",
            }),
            actions=[
                ("[K/r/d]:", "k\n"),
            ],
            input_burst=(
                "Session name (Enter for automatic):",
                "Tasked session\nABC-456\n",
            ),
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertLess(
            output.index("Session name (Enter for automatic):"),
            output.index("No task ID detected on this branch."),
        )
        self.assertLess(
            output.index("No task ID detected on this branch."),
            output.index("Starting copilot..."),
        )
        ingest_calls = self._commands("ingest")
        self.assertEqual(len(ingest_calls), 1)
        self.assertIn("--task=ABC-456", ingest_calls[0]["args"])
        with open(self.sessions_file, encoding="utf-8") as f:
            rows = [line.rstrip("\n").split("|") for line in f if line.strip()]
        tasked_rows = [row for row in rows if row[0] == "tasked-session"]
        self.assertEqual(len(tasked_rows), 1)
        self.assertEqual(tasked_rows[0][1], "Tasked session")

    def test_new_session_with_cwd_mismatch_warns_and_is_not_enrolled(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "wrong-cwd-session",
                "FAKE_SESSION_CWD": os.path.join(self.home, "different-directory"),
            }),
            actions=[("Session name (Enter for automatic):", "\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
        self.assertIn("could not unambiguously identify", output)
        self.assertEqual(self._commands("context-update"), [])
        self.assertFalse(os.path.exists(os.path.join(
            self.state_dir, "wrong-cwd-session", "copilot-s-context.json"
        )))

    def test_multiple_new_sessions_warn_and_never_enroll(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "multiple",
                "FAKE_SESSION_IDS": "new-one,new-two",
            }),
            actions=[
                ("Session name (Enter for automatic):", "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertIn("could not unambiguously identify", output)
        self.assertEqual(self._commands("context-update"), [])
        for session_id in ("new-one", "new-two"):
            self.assertFalse(os.path.exists(os.path.join(
                self.state_dir, session_id, "copilot-s-context.json"
            )))

    def test_legacy_resume_does_not_call_context_helper_or_create_sidecar(self):
        directory = self._session("legacy-session", name="Existing native title")
        workspace = os.path.join(directory, "workspace.yaml")
        with open(workspace, "rb") as f:
            original_workspace = f.read()
        with open(self.sessions_file, "w", encoding="utf-8") as f:
            f.write("legacy-session|Wrapper alias|2026-10-13T10:00:00Z|%s\n" % self.cwd)
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertNotIn("Session name (Enter for automatic):", output)
        with open(self.copilot_log, encoding="utf-8") as f:
            copilot_args = json.load(f)
        self.assertIn("--resume=legacy-session", copilot_args)
        self.assertEqual(
            copilot_args,
            ["--resume=legacy-session", "--model", "auto", "--auto-tier", "balance"],
        )
        self.assertEqual(self._session_rows()[0][1], "Wrapper alias")
        with open(workspace, "rb") as f:
            self.assertEqual(f.read(), original_workspace)
        commands = [call["command"] for call in self._helper_calls()]
        self.assertIn("ingest", commands)
        self.assertNotIn("context-list", commands)
        self.assertNotIn("context-update", commands)
        self.assertFalse(os.path.exists(os.path.join(
            self.state_dir, "legacy-session", "copilot-s-context.json"
        )))

    def test_resumed_launch_forces_auto_and_preserves_non_model_arguments(self):
        self._session("resumed-auto-session")
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
            copilot_s_args=[
                "--model", "caller-model",
                "--auto-tier", "caller-tier",
                "--custom-option", "kept-value",
                "--model=other-caller-model",
                "--auto-tier=other-caller-tier",
                "--another-option=value",
                "positional-argument",
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        with open(self.copilot_log, encoding="utf-8") as f:
            copilot_args = json.load(f)
        self.assertEqual(
            copilot_args,
            [
                "--resume=resumed-auto-session",
                "--custom-option", "kept-value",
                "--another-option=value",
                "positional-argument",
                "--model", "auto",
                "--auto-tier", "balance",
            ],
        )

    def test_resumed_launch_keeps_native_title_and_exact_double_dash_remainder(self):
        directory = self._session("boundary-resume", name="Existing native title")
        workspace = os.path.join(directory, "workspace.yaml")
        with open(workspace, "rb") as f:
            original_workspace = f.read()
        remainder = [
            "--", "--name=Positional title", "--continue", "--model", "positional-model",
            "--auto-tier=positional-tier", "--report", "ABC-999", "--all", "--help",
            "", "  spaced remainder  ", "first line\nsecond line",
        ]
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
            copilot_s_args=["--model=caller-model", "--auto-tier", "caller-tier", *remainder],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertNotIn("Session name (Enter for automatic):", output)
        self.assertEqual(
            self._copilot_args(),
            ["--resume=boundary-resume", "--model", "auto", "--auto-tier", "balance"]
            + remainder,
        )
        self.assertIn('FAKE_COPILOT_TITLE: "Existing native title"', output)
        self.assertEqual(self._commands("report"), [])
        with open(workspace, "rb") as f:
            self.assertEqual(f.read(), original_workspace)
        self.assertEqual(self._session_rows()[0][1], "Existing native title")

    def test_enrolled_resume_refreshes_without_enrolling_again(self):
        self._session("enrolled-session", sidecar=True)
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertLess(output.index(self.CONTEXT_SUMMARY), output.index("[K/r/d]:"))
        calls = self._commands("context-update")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--enroll", calls[0]["args"])
        self.assertEqual(calls[0]["args"][calls[0]["args"].index("--session-id") + 1],
                         "enrolled-session")

    def test_context_update_failure_is_visible_but_keep_flow_continues(self):
        self._session("enrolled-failure", sidecar=True)
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "idle",
                "FAIL_CONTEXT_UPDATE": "1",
            }),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertIn("context metrics update failed", output)
        self.assertIn("session handling continues normally", output)
        self.assertIn("Session kept", output)

    def test_enrollment_verification_rejection_does_not_block_new_session_flow(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "unverified-session",
                "CONTEXT_UPDATE_EXIT": "3",
            }),
            actions=[
                ("Session name (Enter for automatic):", "\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        self.assertIn("simulated context enrollment verification warning", output)
        self.assertIn("context metrics update failed (exit 3)", output)
        self.assertIn("session handling continues normally", output)
        self.assertIn("Session kept", output)
        calls = self._commands("context-update")
        self.assertEqual(len(calls), 1)
        self.assertIn("--enroll", calls[0]["args"])
        self.assertIn("--started-after", calls[0]["args"])
        self.assertFalse(os.path.exists(os.path.join(
            self.state_dir, "unverified-session", "copilot-s-context.json"
        )))

    def test_session_list_aligns_labels_without_changing_index_columns(self):
        ids = ["listed-first", "listed-legacy", "listed-third"]
        self._session(ids[0], sidecar=True, name="First")
        self._session(ids[1], name="Legacy")
        self._session(ids[2], sidecar=True, name="Third")
        with open(self.sessions_file, "w", encoding="utf-8") as f:
            for session_id, name in zip(ids, ("First", "Legacy", "Third")):
                f.write("%s|%s|2026-10-13T10:00:00Z|%s\n" % (session_id, name, self.cwd))
        labels = {
            ids[0]: "ctx 25.0% first-session-label",
            ids[2]: "ctx 150.0% third-session-label",
        }
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "idle",
                "FAKE_CONTEXT_LABELS": json.dumps(labels),
            }),
            actions=[("Select session to resume", "q\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
        import re
        plain_output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        first = plain_output.index("1) First")
        legacy = plain_output.index("2) Legacy")
        third = plain_output.index("3) Third")
        self.assertLess(first, legacy)
        self.assertLess(legacy, third)
        self.assertIn("ctx 25.0% first-session-label", plain_output[first:legacy])
        self.assertNotIn("ctx ", plain_output[legacy:third])
        self.assertIn("ctx 150.0% third-session-label", plain_output[third:])
        calls = self._commands("context-list")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], ["--session-state-dir", self.state_dir])
        with open(self.sessions_file, encoding="utf-8") as f:
            self.assertTrue(all(len(line.rstrip("\n").split("|")) == 4
                                for line in f if line.strip()))

    def test_q_still_exits_without_launching_or_updating_a_session(self):
        self._session("quit-session")
        with open(self.sessions_file, "w", encoding="utf-8") as f:
            f.write("quit-session|Quit me|2026-10-13T10:00:00Z|%s\n" % self.cwd)
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[("Select session to resume", "q\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
        self.assertIn("Bye!", output)
        self.assertFalse(os.path.exists(self.copilot_log))
        self.assertEqual(self._commands("context-update"), [])


class TestRegistryLockSafety(BaseHomeTestCase):
    def setUp(self):
        super().setUp()
        self.registry = os.path.join(self.home, ".copilot-sessions")
        self.original = "lock-fixture|Original alias|timestamp|/worktree\n"
        with open(self.registry, "w", encoding="utf-8") as handle:
            handle.write(self.original)
        self.sourceable = os.path.join(self.home, "launcher-sourceable")
        with open(COPILOT_S, encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(lines[-1].strip(), 'main "$@"')
        with open(self.sourceable, "w", encoding="utf-8") as handle:
            handle.writelines(lines[:-1])

    def _save(self, extra_env=None, session_id="lock-fixture"):
        environment = {
            "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
        }
        environment.update(extra_env or {})
        return run(
            ["bash", "-c",
             'source "$1"; save_session "$2" "Changed alias" "timestamp" "/worktree" true',
             "_", self.sourceable, session_id],
            env=self.env(environment), timeout=15,
        )

    def test_busy_registry_lock_never_allows_an_unlocked_rewrite(self):
        os.mkdir(self.registry + ".lock")
        result = self._save()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        with open(self.registry, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.original, result.stderr)
        self.assertTrue(os.path.isdir(self.registry + ".lock"))

    def test_real_held_flock_blocks_mutation_then_release_allows_save(self):
        lock_path = self.registry + ".lock"
        with open(lock_path, "a", encoding="utf-8") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            inode = os.stat(lock_path).st_ino
            result = self._save()  # Real production ten-second lock timeout.
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("busy", result.stderr)
            self.assertIn("left unchanged", result.stderr)
            with open(self.registry, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), self.original)
            self.assertEqual(os.stat(lock_path).st_ino, inode)
            with open(lock_path, "a", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = self._save()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.original.replace("Original alias", "Changed alias"))
        self.assertEqual(os.stat(lock_path).st_ino, inode, "lock file must persist")

    def test_launcher_inherited_flock_is_held_during_actual_registry_rewrite(self):
        fake_bin = os.path.join(self.home, "fake-bin")
        os.mkdir(fake_bin)
        probe = os.path.join(fake_bin, "mv")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write(
                "#!%s\n"
                "import fcntl, os, sys\n"
                "with open(os.path.join(os.environ['HOME'], '.copilot-sessions.lock'), 'a') as lock:\n"
                "    try:\n"
                "        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "    except BlockingIOError:\n"
                "        pass\n"
                "    else:\n"
                "        sys.exit('launcher lock was released before rewrite')\n"
                "os.execv('/bin/mv', ['/bin/mv', *sys.argv[1:]])\n" % sys.executable
            )
        os.chmod(probe, 0o755)
        result = self._save({
            "PATH": fake_bin + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.registry, encoding="utf-8") as handle:
            self.assertIn("Changed alias", handle.read())
        with open(self.registry + ".lock", "a", encoding="utf-8") as contender:
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _assert_failed_save_preserves_original_and_cleans_only_own_temp(self, command):
        fake_bin = os.path.join(self.home, "failing-bin")
        os.mkdir(fake_bin)
        log_path = os.path.join(self.home, "failed-command.json")
        fake_command = os.path.join(fake_bin, command)
        with open(fake_command, "w", encoding="utf-8") as handle:
            handle.write(
                "#!%s\n"
                "import json, os, sys\n"
                "with open(os.environ['FAIL_COMMAND_LOG'], 'w') as log:\n"
                "    json.dump(sys.argv[1:], log)\n"
                "print('fixture command failure', file=sys.stderr)\n"
                "sys.exit(42)\n" % sys.executable
            )
        os.chmod(fake_command, 0o755)
        sentinel = self.registry + ".unrelated"
        with open(sentinel, "wb") as handle:
            handle.write(b"not this transaction's temp")
        with open(self.registry + ".lock", "a", encoding="utf-8"):
            pass
        original_inode = os.stat(self.registry).st_ino
        expected_paths = {name for name in os.listdir(self.home) if name.startswith(".copilot-sessions.")}
        for session_id in ("lock-fixture", "new-row-fixture"):
            with self.subTest(command=command, session_id=session_id):
                result = self._save({
                    "PATH": fake_bin + ":" + os.path.dirname(sys.executable) + ":/usr/bin:/bin",
                    "FAIL_COMMAND_LOG": log_path,
                }, session_id=session_id)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("fixture command failure", result.stderr)
                with open(self.registry, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), self.original)
                self.assertEqual(os.stat(self.registry).st_ino, original_inode)
                self.assertEqual(
                    {name for name in os.listdir(self.home) if name.startswith(".copilot-sessions.")},
                    expected_paths, "only this transaction's temp may be cleaned",
                )
                with open(sentinel, "rb") as handle:
                    self.assertEqual(handle.read(), b"not this transaction's temp")
                with open(log_path, encoding="utf-8") as handle:
                    arguments = json.load(handle)
                if command == "mktemp":
                    self.assertEqual(arguments, [self.registry + ".XXXXXX"])
                else:
                    self.assertEqual(arguments[0], "-f")
                    self.assertEqual(arguments[-1], self.registry)
                    temporary = arguments[1]
                    self.assertEqual(os.path.dirname(temporary), self.home)
                    self.assertTrue(temporary.startswith(self.registry + "."))
                    self.assertFalse(os.path.lexists(temporary))
                with open(self.registry + ".lock", "a", encoding="utf-8") as contender:
                    fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_mktemp_failure_preserves_old_and_new_row_transactions(self):
        self._assert_failed_save_preserves_original_and_cleans_only_own_temp("mktemp")

    def test_mv_failure_preserves_old_and_new_row_transactions_and_removes_temp(self):
        self._assert_failed_save_preserves_original_and_cleans_only_own_temp("mv")


class TestNormalizeAndValidateTaskFastPathStderr(BaseHomeTestCase):
    """normalize_and_validate_task()'s KEY-123 fast path pipes through
    `xargs` purely to trim whitespace. Malformed input (e.g. containing an
    unbalanced quote) makes `xargs` print its own "unterminated quote"
    diagnostic to stderr; that diagnostic is never an intended validation
    message for the caller (the real validation message is the caller's
    own "Invalid task ID/name ..." echo) and must not leak.

    This sources a trimmed copy of bin/copilot-s (everything up to but not
    including the trailing unconditional `main "$@"` call) so the function
    can be exercised directly without running the whole session manager.
    """

    def setUp(self):
        super().setUp()
        with open(COPILOT_S, encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(lines[-1].strip(), 'main "$@"', "unexpected trailing line in bin/copilot-s")
        self.sourceable = os.path.join(self.home, "copilot-s.sourceable")
        with open(self.sourceable, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])

    def _call(self, raw_input):
        # Invoke the function the same way real call sites do
        # (`if normalized=$(normalize_and_validate_task "$input"); then`),
        # so `set -e` in the sourced script behaves exactly as it does in
        # production instead of aborting on the first internal failure.
        script = (
            f'source "{self.sourceable}"; '
            'if out=$(normalize_and_validate_task "$1"); then '
            "  echo \"OK:$out\"; "
            "else "
            "  echo \"FAIL:$?\"; "
            "fi"
        )
        return subprocess.run(
            ["bash", "-c", script, "_", raw_input],
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_unbalanced_quote_input_never_leaks_xargs_diagnostic(self):
        result = self._call("O'Brien task")
        self.assertNotIn("xargs", result.stderr)
        self.assertNotIn("unterminated quote", result.stderr)

    def test_valid_key_still_accepted_with_clean_stderr(self):
        result = self._call("abc-123")
        self.assertEqual(result.stdout.strip(), "OK:ABC-123")
        self.assertEqual(result.stderr.strip(), "")

    def test_another_unbalanced_quote_variant_stays_clean(self):
        result = self._call('task "unterminated')
        self.assertNotIn("xargs", result.stderr)
        self.assertNotIn("unterminated quote", result.stderr)


if __name__ == "__main__":
    unittest.main()
