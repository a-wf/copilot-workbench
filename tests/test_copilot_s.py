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

    def _resolved_helper_path(self, copilot_s_path, extra_env=None):
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
            if line.startswith("+ TASK_REPORT_HELPER="):
                return line.split("=", 1)[1]
        self.fail(f"TASK_REPORT_HELPER assignment not observed in:\n{result.stderr}")

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


class TestInstallerHelpNotTruncated(unittest.TestCase):
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
        result = run([INSTALL_SH, "--help"], env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        header_lines = self._header_comment_lines(INSTALL_SH)
        # Every non-trivial line of the header comment must appear in the
        # rendered help output (this catches any fixed-range truncation).
        for line in header_lines:
            text = line.lstrip("#").strip()
            if text:
                self.assertIn(text, result.stdout, f"missing header line: {text!r}")

    def test_uninstall_help_prints_full_header(self):
        result = run([UNINSTALL_SH, "--help"], env=dict(os.environ))
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
    check conservative enrollment, list alignment, and stop-time handling."""

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
                "root = pathlib.Path(os.environ['HOME']) / '.copilot' / 'session-state'\n"
                "mode = os.environ.get('FAKE_COPILOT_MODE', 'idle')\n"
                "if mode in ('new', 'multiple'):\n"
                "    session_ids = os.environ.get('FAKE_SESSION_IDS', 'fresh-session').split(',')\n"
                "    for session_id in session_ids:\n"
                "        directory = root / session_id\n"
                "        directory.mkdir(parents=True, exist_ok=True)\n"
                "        created = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')\n"
                "        cwd = os.environ.get('FAKE_SESSION_CWD', os.getcwd())\n"
                "        (directory / 'workspace.yaml').write_text(\n"
                "            'created_at: ' + created + '\\n' + 'cwd: ' + cwd + '\\n' + 'name: Fake session\\n',\n"
                "            encoding='utf-8')\n"
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

    def _run_tty(self, env, actions=(), timeout=20):
        """Run the real script with a controlling terminal and answer prompts
        only when they appear, so flush_stdin cannot consume later answers."""
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(self.cwd)
            os.execve("/bin/bash", ["/bin/bash", COPILOT_S], env)
        output = bytearray()
        actions = list(actions)
        action_index = 0
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
                    while action_index < len(actions):
                        marker, response = actions[action_index]
                        if marker.encode() not in output:
                            break
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
        return status, output.decode("utf-8", errors="replace"), action_index

    def _helper_calls(self):
        if not os.path.exists(self.helper_log):
            return []
        with open(self.helper_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _session(self, session_id, *, sidecar=False, name=None):
        directory = os.path.join(self.state_dir, session_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "workspace.yaml"), "w", encoding="utf-8") as f:
            f.write(
                "created_at: 2026-10-13T10:00:00Z\n"
                "cwd: %s\n"
                "branch: ABC-123\n"
                "name: %s\n"
                % (self.cwd, name or session_id)
            )
        if sidecar:
            with open(os.path.join(directory, "copilot-s-context.json"), "w", encoding="utf-8") as f:
                f.write("{}\n")
        return directory

    def _commands(self, name):
        return [call for call in self._helper_calls() if call["command"] == name]

    def test_fresh_session_enrolls_exact_id_and_prints_metrics_before_keep_prompt(self):
        status, output, actions = self._run_tty(
            self._env({
                "FAKE_COPILOT_MODE": "new",
                "FAKE_SESSION_IDS": "fresh-session",
            }),
            actions=[("[K/r/d]:", "k\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
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

    def test_new_session_with_cwd_mismatch_warns_and_is_not_enrolled(self):
        status, output, _ = self._run_tty(self._env({
            "FAKE_COPILOT_MODE": "new",
            "FAKE_SESSION_IDS": "wrong-cwd-session",
            "FAKE_SESSION_CWD": os.path.join(self.home, "different-directory"),
        }))
        self.assertEqual(status, 0, output)
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
            actions=[("[K/r/d]:", "k\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
        self.assertIn("could not unambiguously identify", output)
        self.assertEqual(self._commands("context-update"), [])
        for session_id in ("new-one", "new-two"):
            self.assertFalse(os.path.exists(os.path.join(
                self.state_dir, session_id, "copilot-s-context.json"
            )))

    def test_legacy_resume_does_not_call_context_helper_or_create_sidecar(self):
        self._session("legacy-session")
        status, output, actions = self._run_tty(
            self._env({"FAKE_COPILOT_MODE": "idle"}),
            actions=[
                ("Select session to resume", "1\n"),
                ("[K/r/d]:", "k\n"),
            ],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 2, output)
        with open(self.copilot_log, encoding="utf-8") as f:
            copilot_args = json.load(f)
        self.assertIn("--resume=legacy-session", copilot_args)
        commands = [call["command"] for call in self._helper_calls()]
        self.assertIn("ingest", commands)
        self.assertNotIn("context-list", commands)
        self.assertNotIn("context-update", commands)
        self.assertFalse(os.path.exists(os.path.join(
            self.state_dir, "legacy-session", "copilot-s-context.json"
        )))

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
            actions=[("[K/r/d]:", "k\n")],
        )
        self.assertEqual(status, 0, output)
        self.assertEqual(actions, 1, output)
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
