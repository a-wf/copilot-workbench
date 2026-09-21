# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `instructions/copilot-instructions.md`: added a "Cost-saving: skip
  unnecessary stages" section instructing the orchestrator to skip
  pipeline stages (planner/reviewer/tester/test-reviewer) for small,
  low-risk, easily-verified tasks, to reduce token/money spend, while
  keeping the full pipeline for non-trivial or higher-risk changes.

### Fixed

- `scripts/update.sh` no longer errors under `set -u` on macOS's stock
  Bash 3.2 `/bin/bash` when invoked with no installer arguments (an empty
  array expansion that throws "unbound variable" on Bash 3.2 even though
  it's fine on Bash 4+).
- `scripts/update.sh` no longer silently drops installer arguments such as
  `--copy`/`--prefix` — it now passes them straight through to
  `install.sh` (the legacy `--` separator is still accepted, but is no
  longer required).
- `bin/copilot-jira-report.py`: an explicit
  `"cache_read_per_million": null` in `model-pricing.json` (as opposed to
  the key being absent) now correctly falls back to `input_per_million`
  instead of propagating `None` into the cost arithmetic.

### Added

- `scripts/update.sh --skip-pull` to skip `git pull` and just re-run the
  installer — useful offline, or for hermetic tests.
- `scripts/install.sh --prefix` now rejects an empty value and
  canonicalizes relative paths to absolute ones, so the install manifest
  (and therefore `scripts/uninstall.sh`) works correctly regardless of the
  current directory at install vs. uninstall time.
- `tests/test_update.sh`: hermetic (no-network) smoke tests for
  `scripts/update.sh` against a local bare-repo fixture, covering no-args,
  direct arg passthrough, the legacy `--` separator, `--help`, and the
  error path for unknown arguments. Wired into CI on both Ubuntu and
  macOS, plus a macOS-only job step that exercises the same flows via the
  system `/bin/bash` explicitly (Bash 3.2), since GitHub's macOS runners
  otherwise default to a newer Homebrew `bash` on `PATH`.
- CI: `scripts/install.sh --prefix` hardening smoke test (empty value
  rejected; relative prefix resolves/uninstalls correctly across a
  changed working directory).

### Documented

- README: `copilot-s --version`/`-v` usage, and an explicit "no automatic
  retention policy" note for OTEL span files/telemetry state — users
  should not delete active telemetry before it's ingested; safe periodic
  pruning is a known, deliberately-unimplemented gap rather than something
  this toolkit currently does automatically.

## [1.0.0] - 2026-09-21

### Added

- Initial public packaging of the previously personal `copilot-s` session
  manager and `copilot-jira-report.py` Jira usage reporter as an
  installable, symlink-based toolkit.
- 6-role multi-agent implementation pipeline
  (`planner`/`coder`/`reviewer`/`fixer`/`tester`/`test-reviewer`) and
  orchestrator instructions.
- `scripts/install.sh`, `scripts/uninstall.sh`, `scripts/update.sh` with
  `--dry-run`/`--copy`/`--restore-backups` support.
- `COPILOT_JIRA_REPORTS_DIR` environment variable to override the Jira
  usage reports directory (previously hardcoded to
  `~/Desktop/CopilotJiraTaskReports`).
- CI (GitHub Actions) running on Ubuntu and macOS: bash syntax/shellcheck,
  Python compile + full test suite, and install/uninstall smoke tests.

### Changed

- Portable timestamp formatting in `copilot-s` (works with both BSD date on
  macOS and GNU date on Linux), replacing a macOS-only `date -j -f` call.

## Prior history

This toolkit existed as personal, machine-local scripts before this
repository was created; see `docs/architecture.md` for the current design.
No prior released versions exist.
