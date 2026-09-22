# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Documentation

- Added a neutral comparison of Copilot `/model auto` and this toolkit,
  explaining that Auto provides adaptive GitHub-managed model selection while
  the toolkit provides inspectable workflow policy, session management, task
  reporting, role separation, and bounded cost controls. The README now
  recommends a hybrid setup for most users and explicitly documents that the
  custom-agent pipeline is instruction-driven rather than a deterministic
  workflow engine.

### Changed — agent pipeline redesign (7 roles, cost-aware routing)

This redesigns the multi-agent pipeline for lower token/cost overhead and
stricter role boundaries. It is agent-config-only: no changes to
`copilot-s` or the task usage reporter's behavior, other than updated
agent-name references used for historical-session effort inference.

- **New agents:**
  - `discovery` (`gemini-3.8-flash`, `read`/`search` tools only) — read-only
    broad/unfamiliar codebase mapping. Never plans, writes code, reviews,
    tests, or runs shell commands.
  - `senior-coder` (`claude-sonnet-5`) — complex implementation and
    targeted fixes (multi-file architecture, async state, schema/data-model
    changes, deep structural bugs, new services). Escalations from
    `coder` hand off the current diff/context and continue rather than
    restarting from scratch.
- **Retired:** `agents/fixer.agent.md` is deleted. `coder`/`senior-coder`
  now apply their own targeted fixes directly, always at the same tier
  that did the original implementation.
- **Upgrading:** after pulling this release, run `scripts/update.sh` (or
  `scripts/install.sh` again) once — a plain `git pull` alone does not
  remove the retired `fixer` agent's already-installed path
  (`~/.copilot/agents/fixer.agent.md`), because that removal is an
  install/update-time migration step, not something a symlink refresh
  performs on its own. See "Upgrading to this release" in README.md.
- **`coder`** now targets `kimi-k2.7-code` (was `claude-haiku-4.5`) and is
  scoped to routine implementation/fixes (CRUD, UI, standard logic);
  anything more structural routes to `senior-coder`.
- **`planner`** is now explicitly optional and ambiguity/design-only; it
  consumes `discovery`'s output instead of duplicating broad exploration,
  and its output includes a routing hint (routine vs. complex) for
  `coder`/`senior-coder`.
- **`reviewer`** now does one comprehensive review, then at most 3 bounded
  focused-verification rounds that check only prior findings and
  regressions introduced by fixes — never a second broad review — and
  escalates to the user if unresolved after round 3.
- **`tester`** now only runs when behavior merits testing, with a bounded
  test/fix loop capped at 3 rounds before escalating; never implements
  fixes itself.
- **`test-reviewer`** is now scoped to complex/high-risk tasks only, with
  the same bounded one-pass-plus-3-rounds verification discipline as
  `reviewer`.
- `instructions/copilot-instructions.md` is rewritten around explicit task
  tiers (trivial/small/standard/complex/high-risk) and a stage-selection
  matrix: every stage is optional, skips are briefly disclosed to the
  user, and no stage duplicates work another stage already did.
- Every agent's frontmatter `description` and prompt body now explicitly
  reinforces bounded scope, concise output, and avoiding duplicated work,
  as part of the pipeline's cost-control design.
- `scripts/install.sh` safely removes a previously-installed
  `~/.copilot/agents/fixer.agent.md` (only when it's manifest-managed or a
  symlink into this repo checkout, pruning the install manifest
  atomically) while leaving an unmanaged/user-owned file at that path
  untouched. `discovery`/`senior-coder` install automatically via the
  existing `agents/*.agent.md` glob — no installer change was needed to
  add them.
- `bin/copilot-task-report.py`'s custom-agent effort-inference map adds
  `discovery`/`senior-coder` and reflects `coder`'s new (cheaper) model
  tier; the retired `fixer` entry is kept solely so historical sessions
  recorded before this change still get a reasonable inferred effort
  label.
- README, `docs/architecture.md` (including the pipeline diagram), and
  this changelog are updated for the 7-role pipeline. The changelog's
  historical `1.0.0`/`2.0.0` entries are left as-is: they describe the
  pipeline as it existed at the time and are not retroactively edited.

## [2.0.0] - 2026-09-21

This is a **breaking** release: the usage reporter and its storage layout
are now generic (task-based) instead of Jira-specific. See "Migration from
1.x" below.

### Breaking changes

- `bin/copilot-jira-report.py` is renamed to `bin/copilot-task-report.py`.
  There is no compatibility shim, no `--jira` flag, and no
  `COPILOT_JIRA_*` environment variables — these are removed outright, not
  deprecated.
- Storage moves from `~/.copilot/jira-reports/` to
  `~/.copilot/task-reports/`, and the per-item JSON directory moves from
  `tickets/` to `tasks/`. The Desktop reports directory moves from
  `~/Desktop/CopilotJiraTaskReports` to `~/Desktop/CopilotTaskReports`.
  `COPILOT_JIRA_REPORTS_DIR` is replaced by `COPILOT_TASK_REPORTS_DIR`
  (no fallback to the old name).
- `copilot-s`'s `--report TASK_ID` flag is unchanged in meaning; a new
  `--task TASK_ID` alias is also accepted. Internal env var names change
  (`COPILOT_JIRA_REPORT_HELPER` -> `COPILOT_TASK_REPORT_HELPER`,
  `COPILOT_JIRA_REPORTS_DIR` -> `COPILOT_TASK_REPORTS_DIR`).

### Migration from 1.x

Migration is **automatic and idempotent** — running `scripts/install.sh`
(or simply using `copilot-s`/`copilot-task-report.py`, which trigger the
same migration on startup) will:

- Copy `~/.copilot/jira-reports/install-marker.json` and
  `model-pricing.json` to `~/.copilot/task-reports/` if not already
  present there (your pricing edits are preserved, never overwritten).
- Merge `session-state.json` entries into the new location, renaming each
  entry's `jira_key` field to `task_id` (offsets/timestamps preserved).
- Migrate every `tickets/<KEY>.json` file to `tasks/<key>.json`
  (normalizing the ID and converting any `jira_key` field in the JSON body
  to `task_id`).
- Merge `~/Desktop/CopilotJiraTaskReports` into
  `~/Desktop/CopilotTaskReports`: identical files are deduplicated;
  files that exist at both locations with *different* content are never
  overwritten — both copies are kept (the legacy one renamed with a
  `.legacy-conflict` suffix) and a warning is printed.
- Only remove each legacy source (directory or file) once its contents
  are verifiably preserved at the new location; if anything can't be
  migrated cleanly, the legacy source is left in place (safe to retry —
  already-migrated items are never re-copied or overwritten).
- `scripts/install.sh` additionally removes a stale
  `~/.local/bin/copilot-jira-report.py`, but **only** if it is either
  listed in this toolkit's own install manifest or a symlink resolving
  into this repository — an unrelated file at that path is left alone.
- **Known limitation:** the Desktop-reports migration only looks in the
  hardcoded default pre-2.0 location (`~/Desktop/CopilotJiraTaskReports`).
  If you had customized the now-removed `COPILOT_JIRA_REPORTS_DIR` to
  point somewhere else, that custom location is not auto-discovered — move
  your old reports to the default path yourself before upgrading if you
  want them migrated automatically (see the README's "Limitations"
  section for the full remediation steps).

No manual steps are required. `scripts/uninstall.sh` never deletes usage
reports, task state, or your pricing config, exactly as before.

### Added

- `instructions/copilot-instructions.md`: added a "Cost-saving: skip
  unnecessary stages" section instructing the orchestrator to skip
  pipeline stages (planner/reviewer/tester/test-reviewer) for small,
  low-risk, easily-verified tasks, to reduce token/money spend, while
  keeping the full pipeline for non-trivial or higher-risk changes.
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

### Fixed

- `scripts/update.sh` no longer errors under `set -u` on macOS's stock
  Bash 3.2 `/bin/bash` when invoked with no installer arguments (an empty
  array expansion that throws "unbound variable" on Bash 3.2 even though
  it's fine on Bash 4+).
- `scripts/update.sh` no longer silently drops installer arguments such as
  `--copy`/`--prefix` — it now passes them straight through to
  `install.sh` (the legacy `--` separator is still accepted, but is no
  longer required).
- `bin/copilot-task-report.py` (formerly `copilot-jira-report.py`): an
  explicit `"cache_read_per_million": null` in `model-pricing.json` (as
  opposed to the key being absent) now correctly falls back to
  `input_per_million` instead of propagating `None` into the cost
  arithmetic.
- Desktop-reports migration is now robust against a legacy directory that
  stays behind across multiple runs (e.g. blocked by an unresolved
  symlink): repeated runs no longer mint additional
  `.legacy-conflict-2.md`, `.legacy-conflict-3.md`, etc. for content
  that's already been preserved by a previous run.
- Only the canonical top-level `<task-id>.md` (the flat layout written by
  the current tool) is ever fully re-rendered from live `tasks/*.json`
  state during Desktop-reports migration. Nested/archived legacy copies
  (e.g. a user's own `archive/` subfolder) are never re-rendered against
  current task data — at most their heading is safely rewritten, so
  point-in-time snapshot content/totals are preserved as-is.
- `_preserve_verbatim`'s directory-copy path now verifies full recursive
  content equality before treating an existing destination as
  already-migrated, and copies into a temporary directory before an
  atomic rename into place, instead of trusting destination existence
  alone (which could have silently accepted a partial/interrupted prior
  copy and then deleted the legacy source).
- `copilot-s`'s fast-path task ID normalization no longer leaks `xargs`'s
  own "unterminated quote" diagnostic to stderr for malformed input
  (e.g. an unbalanced quote); such input now falls through cleanly to the
  normal invalid-task-ID handling.
- Task IDs consisting only of dots (e.g. `.`, `..`, `...`) are now
  rejected by `normalize_task_id`, in addition to the existing rejection
  of any ID containing a literal `..` path-traversal segment.
- Both legacy-migration provenance markers
  (`.legacy-tickets-migrated.json` and `.legacy-desktop-migrated.json`)
  are now pruned the moment their corresponding legacy source tree is
  successfully removed, instead of persisting indefinitely. Those
  markers exist only to bridge a migration that was blocked across
  multiple runs while the legacy source persisted; once the source is
  actually gone, a later-restored legacy tree (e.g. from a backup) is
  now always treated as a brand-new occurrence — freshly compared
  against the current destination and conflict-flagged/preserved as
  needed — rather than silently skipped via a stale fingerprint left
  over from a migration that had already fully completed.
- Desktop-reports migration no longer risks silently swallowing a
  genuinely different legacy `.md` report just because a full live
  re-render of the current task state happens to already match the
  destination file. The content used to detect "already migrated"
  duplicates and to populate `.legacy-conflict` files is now always the
  raw legacy source (at most heading-only rewritten), completely
  separate from the canonical, possibly fully-re-rendered content only
  ever used to populate a destination that doesn't exist yet — so a
  `.legacy-conflict` file always preserves the real historical legacy
  record, never a re-render of unrelated live state.
- Desktop-reports migration now explicitly creates a destination
  directory for every legacy subdirectory it walks, even ones that
  contain no files anywhere in their own subtree, so empty legacy
  directory structure is preserved instead of being silently lost
  before the legacy tree is removed.

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
