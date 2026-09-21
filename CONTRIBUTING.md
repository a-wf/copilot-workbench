# Contributing

Thanks for considering a contribution to copilot-cli-toolkit.

## Development setup

```bash
git clone https://github.com/<you>/copilot-cli-toolkit.git
cd copilot-cli-toolkit
```

No build step or dependencies to install — the Python helper is stdlib-only
and the shell scripts run with the system `bash`.

## Running checks locally

```bash
# Bash syntax
bash -n bin/copilot-s scripts/install.sh scripts/uninstall.sh scripts/update.sh tests/test_update.sh

# shellcheck (install via your package manager, e.g. `brew install shellcheck`
# or `apt-get install shellcheck`)
shellcheck -S warning bin/copilot-s scripts/install.sh scripts/uninstall.sh scripts/update.sh tests/test_update.sh

# Python compile + test suite
python3 -m py_compile bin/copilot-task-report.py tests/test_copilot_task_report.py tests/test_copilot_s.py
python3 -m unittest discover -s tests -v

# Install/uninstall smoke test against a throwaway HOME — never run against
# your real HOME during development.
TESTHOME=$(mktemp -d)
HOME="$TESTHOME" bash scripts/install.sh --dry-run
HOME="$TESTHOME" bash scripts/install.sh
HOME="$TESTHOME" bash scripts/uninstall.sh --dry-run
rm -rf "$TESTHOME"

# update.sh smoke test — hermetic (a local bare-repo fixture, no network),
# covers no-args, direct arg passthrough (--copy etc.), the legacy `--`
# separator, --help, and --skip-pull.
bash tests/test_update.sh
```

All of the above run in CI (`.github/workflows/ci.yml`) on both Ubuntu and
macOS runners, plus a macOS-only step that repeats the critical bash
scripts explicitly under `/bin/bash` (GitHub's macOS runners otherwise
default to a newer Homebrew `bash` on `PATH`, which would not catch
Bash-3.2-specific regressions); please make sure they pass before opening
a PR.

## Code conventions

- **Bash**: target Bash 3.2 compatibility (macOS ships an old default
  `/bin/bash`) — no associative arrays, no `mapfile`/`readarray`. Prefer
  `date +%Y%m%d-%H%M%S` (works on both BSD and GNU `date`) over
  flag-specific date parsing; if you must parse a specific format, support
  both BSD (`date -j -f`) and GNU (`date -d`) as `bin/copilot-s`'s
  `format_timestamp()` does.
- **Python**: stdlib-only, no third-party dependencies. Keep
  `bin/copilot-task-report.py` a single self-contained script.
- **Tests**: hermetic — no writes outside a per-test temp directory, no
  network access, no dependency on any specific machine's real
  `~/.copilot` state. Module/script paths under test should be resolved
  relative to the repo root (see `tests/test_copilot_task_report.py`'s
  `REPO_ROOT`/`MODULE_PATH`/`SCRIPT_PATH` constants), not hardcoded to an
  installed location.
- **No personal/machine-specific paths, IDs, or data** in anything checked
  in: no real task IDs, no real session IDs, no `/Users/<name>`
  paths, no usage/report data from a real machine. Use synthetic examples.

## Submitting changes

1. Open an issue first for anything non-trivial (new feature, behavior
   change) so the approach can be discussed.
2. Keep PRs focused — unrelated cleanups should be a separate PR.
3. Update `README.md`/`docs/architecture.md` if behavior changes.
4. Add or update tests for any change to `bin/copilot-task-report.py` or
   `bin/copilot-s`'s testable logic.
5. Add a `CHANGELOG.md` entry under "Unreleased".
