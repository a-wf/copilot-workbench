# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed — independent review for every code change

- Require one independent reviewer pass for every coherent source/code
  modification batch, including production source, tests (including
  tester-authored tests), scripts, Storybook stories, and executable or
  behavior-affecting configuration, even when no executable behavior is
  added. Pure prose/documentation and mechanical git-only operations are
  normally exempt. Prefer review after the full code-and-test batch is
  available; later tester-authored code requires bounded focused
  verification, not a redundant broad review or budget reset. If the
  four-call budget is exhausted with new code unchecked, stop and
  escalate. Route findings to the original implementer; tester-authored
  test defects return to tester, while production-code defects return to
  the original production author. Keep the review budget at one
  comprehensive pass plus at most three focused verification rounds;
  preserve existing routing approval requirements.
- Default routine coder/GPT-6 Luna changes to Claude Sonnet 5.5 at high
  effort/long context; use Opus 5.5 high/long-context for senior-coder
  authorship, complex/high-risk work, or unknown implementation model.
  This is a toolkit task-routing convention, not native Copilot CLI
  configuration. The model recommendation is provisional, not based on
  code-review benchmark evidence. See the reviewer profile for dated
  Artificial Analysis Intelligence Index values (general-intelligence
  scores, not high-effort measurements or code-review benchmarks) and official GitHub token
  pricing references; actual cost varies with tokens, caching, and output
  verbosity.
- Expanded reviewer scope to call out defensive correctness edge cases,
  including whitespace-only accessibility labels, input validation,
  TypeScript narrowing/build mismatches, and Storybook control-to-prop
  boundaries. Review remains high-confidence, not a guarantee of catching
  all bugs.

### Changed — approval before substantive routed work

- The orchestrator now requires explicit routing approval before substantive
  work on standard, complex, or high-risk tasks, and before broad discovery
  or design planning at any tier. It presents the proposed stages and
  configured agent/model/effort/context, every skipped group with reasons,
  and choices for recommended delegation, main-session handling, or custom
  routing. Only minimal classification reads are allowed first; cancellation
  or decline stops the work.
- Clarified that routing approval is distinct from plan/action approval,
  significant route changes require renewed approval, and unavailable
  approval interaction pauses rather than silently proceeding. Small/casual
  implementation keeps its automatic coder route, and simple questions stay
  direct. This is an instruction/config convention, not deterministic CLI
  enforcement.

### Changed — official GitHub per-token pricing, refreshed automatically

- The report's estimated USD cost now uses GitHub's **official** per-token
  rates from [Models and pricing for GitHub Copilot](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
  (input, cached input, cache write, output; Default and Long-context
  tiers). `copilot-task-report.py` fetches the docs article-body API (stdlib
  `urllib`, 1 MB cap, 10s timeout per socket operation and a 20s total
  deadline checked between body reads — best-effort limits, not a hard
  deadline: a slowly streaming server, slow DNS resolution or a
  pricing-lock wait can exceed the nominal timeout; only when Python cannot
  verify the TLS certificate it retries once via the system `curl`, with
  certificate verification still on, HTTPS only, no redirects, and the same
  limits, and notes this on stderr), parses every
  provider table strictly, and caches a validated snapshot (fetch time,
  source URL, content SHA-256) in
  `~/.copilot/task-reports/official-pricing-cache.json` for 24h. Refresh
  runs at most once per `ingest`/`report` command, under its own lock,
  before the ingest lock; render/import/`ensure-marker` never fetch. A
  failed fetch or unparseable page keeps the last valid cache (labeled
  **stale** in the report), warns on stderr, and suppresses retries for 1h
  (retried at most hourly while failing, not once per 24h). A snapshot more
  than 7 days past its fetch time (download time, not a rate effective
  date) is kept for reference only: newly ingested calls are recorded as
  unpriced with a fixed reason. A corrupted cache is rejected whole (every
  field used later is validated, incl. notes, names, lookup and
  `snapshot_id`/hash consistency) and re-fetched; a corrupted refresh-state
  file is reset with a warning without affecting the cache or task costs.
  `COPILOT_TASK_REPORT_PRICING_FETCH=0` disables the network;
  `PRICING_FETCHER` is an injectable transport for fixtures. No rate is
  hardcoded.
- Each call is priced **at ingestion**, with the tier chosen from that
  call's own input tokens, and recorded additively in the task JSON
  (`official_cost`: per-model buckets keyed by snapshot id plus unpriced
  counts by reason, snapshot metadata stored once) — a later refresh never
  reprices past estimates. Cache-read and cache-write tokens are treated as
  subsets of input (verified on real Copilot CLI OTEL spans), reasoning
  tokens are priced once as part of output. Unlisted models, missing
  input/output counts, inconsistent usage, tier-ambiguous calls,
  cache-write tokens without a listed rate, reasoning > output, and every
  Gemini/Google-provider call with reasoning tokens (its output may exclude
  reasoning) are reported as unpriced with a reason (partial lower bound,
  never $0; token counts kept).
  Calls recorded before this existed are shown as **legacy** (token counts
  kept, never backfilled or repriced).
- Removed the "Company Fixed Per-Request Charge" section from the default
  report: its rates were partly benchmark-derived (Artificial Analysis) and
  misleading next to official pricing. `request-pricing.json`, the
  fixed-charge helpers and the recorded `by_model_effort` data are kept,
  dormant. The old approximate `model-pricing.json` table is likewise
  inactive; installed copies are never modified, migrated or deleted, and
  the installer still copies both only if absent.
- The By Model table gains a cache-write column; the summary's cache-write
  row is now a subset of input tokens rather than "informational".

### Changed — mandatory small implementation delegation

- Added structured `task_routing.small_casual_implementation` defaults for
  invoking the named custom agent `coder` through the task tool
  (`gpt-6-luna`, low reasoning effort, default context). Clarified in the
  orchestrator instructions and README that the main session coordinates
  and oversees delegated implementation, while `coder` does not test or
  review; reviewer/tester remain optional according to risk. Informational
  questions stay direct, and routine shell/mechanical work stays on the
  separate built-in `task` route. The seven custom-agent inventory is
  unchanged.

### Changed — low-cost mechanical task delegation

- Added a separate built-in `task` route for bounded shell/git mechanics
  (`gpt-6-luna`, low effort, default context), so routine command execution
  is delegated for cost savings even when no coding-agent stage is needed.
  It is a Copilot CLI built-in, not an eighth toolkit custom agent; the
  seven `agents/*.agent.md` roles remain unchanged.
- Clarified that `discovery` is read-only mapping, never the command or
  commit runner, and that the main session initiates Task delegation and
  retains oversight. Commits/pushes require explicit user authorization;
  authorized commits must preserve unrelated dirty edits and stage only
  the requested changes. Unavailable tools or unsafe
  security-sensitive/complex judgment stay in the main session with the
  reason communicated.

### Added — company fixed per-request charge

- New, separate "Company Fixed Per-Request Charge" report section: a
  company-configured policy charging a fixed USD amount per **model
  request**, keyed by model + reasoning-effort level, using
  company-configured fixed rates with the source listed per model. Clearly labeled
  as **not** official GitHub pricing and never a verified bill; kept
  independent of (and never added to) the existing approximate token-based
  USD estimate, which is unchanged.
- New `config/request-pricing.json`, installed to
  `~/.copilot/task-reports/request-pricing.json` **only if absent** (edits
  preserved, same pattern as `model-pricing.json`). Defaults:
  `gpt-6-luna` xhigh $0.04; `gpt-6.1-sol` medium $0.21, xhigh $0.39
  (company price list, 2026-09-30 screenshot; per-request billing
  user-confirmed 2026-10-05); `claude-opus-5.5` high $1.82 (AA high /
  Default Fallback); `gemini-3.8-flash` low explicitly **unpriced** (AA
  publishes no cost/task figure).
- Config validation: rates must be finite, nonnegative numbers or `null`
  (booleans, strings, negative, NaN/Infinity, and numeric literals too
  large/long to convert rejected); an invalid file is
  rejected as a whole with an explicit error, and a missing file is shown
  as *unavailable* — never as $0.
- Reports now track a joint model × effort aggregation (`by_model_effort`)
  and render a model+effort breakdown with per-source subtotals
  (measured / configured-estimate / inferred-estimate), partial-total
  labeling, and missing-rate warnings. Unknown effort is never priced.
  Requests recorded before joint tracking existed are reported as
  unattributed (no backfill). "Requests" are OTEL usage-bearing model spans,
  including retried/failed calls that reported usage — the report does not
  claim every request was captured.

### Changed — October routing refresh

- Updated planner to GPT-6.1 Sol with medium effort and coder/tester to
  GPT-6 Luna with xhigh effort. Kept senior-coder, reviewer, and
  test-reviewer on Claude Opus 5.5/high, and discovery on Gemini 3.8
  Flash/low. Context tiers are unchanged.
- Synced agent profiles, structured routing, loaded instructions,
  documentation, tests, and the reporter's fallback effort labels.

### Changed — explicit agent-stage communication

- Strengthened the orchestrator instructions so stage skipping can no
  longer be an invisible internal decision. Before substantive work it
  must announce the task tier, the stage being run (or direct main-session
  handling), and every skipped stage/group with a short reason.
- Skip decisions made later must be communicated at the transition rather
  than only after completion, and final responses now include a compact
  `Stages:` record of what ran and what was skipped. Pure informational
  requests use the same rule in a single concise sentence.

### Changed — model/effort remap (Sept 2026 cost-efficiency tiers)

- Remapped agent default models to a 3-tier cost/capability scheme aligned
  with an internal cost-efficiency alignment reference (start cheap for
  routine work, step up for design/routing, reserve the top tier for hard
  problems): `coder`/`tester` now default to `gpt-6-luna` (was
  `kimi-k2.7-code`), `planner` now defaults to `gpt-6-sol` (was
  `claude-sonnet-5`), and `senior-coder`/`reviewer`/`test-reviewer` now
  default to `claude-opus-5.5` (was `claude-sonnet-5`/`claude-opus-5`).
  `discovery` keeps `gemini-3.8-flash` — its value is context-window size
  for broad codebase mapping, not raw task-solving capability, so it's
  exempt from this tiering.
- Added a per-agent `reasoningEffort` frontmatter default (supported by
  CLI v1.0.66+): `max` for `coder`/`tester`, `high` for
  `planner`/`senior-coder`/`reviewer`/`test-reviewer`, `low` for
  `discovery`. These are shipped repository defaults only — override per
  installation via `/subagents`, not by editing `agents/*.agent.md`.
- Added `gpt-6-luna`, `gpt-6-sol`, and `claude-opus-5.5` pricing entries
  (plus aliases) to `config/model-pricing.json`.
- Updated `CUSTOM_AGENT_EFFORT_MAP` in `bin/copilot-task-report.py` (the
  static fallback used only when no measured/configured effort telemetry
  is available) to match the new defaults.
- Updated `tests/test_agent_pipeline.py`'s `EXPECTED_AGENTS` and
  `tests/test_copilot_task_report.py`'s inferred-effort fixture to match.
- Added `config/agent-routing.yaml` as a structured toolkit routing table
  for hard task-call defaults (`model`, `reasoning_effort`,
  `context_tier`), mirrored the same table in
  `instructions/copilot-instructions.md`, and updated the installer/tests
  to keep it synced locally at `~/.copilot/agent-routing.yaml`.

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
