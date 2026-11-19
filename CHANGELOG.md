# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added — permanent purge of one quarantine batch (menus `x` and `r` → `4`, quarantine-purge-c1)

- New shared stdlib engine `bin/copilot_quarantine_purge.py` (a library,
  installed next to the cleanup helpers by `scripts/install.sh` for both
  symlink and `--copy` installs; removed by `uninstall.sh` with the other
  managed files). Both helpers load it lazily from their own real
  directory; existing subcommands do not need it.
- New subcommands in `copilot-session-cleanup.py` and
  `copilot-report-cleanup.py`: `purge-list` (read-only;
  `PURGEABLE<TAB><batch>`), `purge-preview --batch B` (read-only exact
  inventory, estimated reclaimable space = Σ `st_blocks`×512, snapshot;
  `PURGE_SNAPSHOT<TAB><kind><TAB><batch><TAB><digest>` or
  `PURGE_INELIGIBLE<TAB><batch>`), and `purge --batch B [--snapshot D]`
  (interactive, `/dev/tty` only, exact phrase
  `PURGE <sessions|reports> <batch> <12-hex snapshot>`, 120 s, no
  yes/force bypass). Existing commands, flags and machine lines are
  unchanged.
- Only fully settled batches (every entry `moved`/`skipped`, ≥1 moved,
  payload fingerprints unchanged, nothing extra) of plain private single-link
  files on one mount, with nothing open per a system `lsof`, can be purged.
  The plan is rebuilt under the quarantine's existing lock (session-list
  lock / `.transaction.lock`) and must match the confirmed snapshot before
  the first deletion; a journal `<quarantine>/.purge/<batch>.json`
  (`started` → `complete`/`partial`) is written first and kept.
  Bottom-up descriptor-relative deletion of inventoried entries only, with
  identity and parent-binding re-checks; stops at the first discrepancy.
- `copilot-s`: new menu `x) Permanently purge a quarantined-session batch
  (irreversible)` in the session list and the no-sessions menu (listed, not
  in the short prompt), and `4)` in the `r` submenu (prompt unchanged). Never
  launches Copilot.
- Changed: session `restore` now performs its moves under the session-list
  lock; both `restore` commands refuse a batch with a purge journal (checked
  before the plan and again under the lock); both `list` commands append
  `[permanent purge started: not restorable]` to such batches (machine
  lines unchanged).
- Limitations: macOS and Linux only; estimate only (APFS clones/snapshots,
  open files); not secure erasure; a hostile same-user swap between the
  final check and `unlink(2)` cannot be excluded portably; Linux
  `chattr +i`/ACLs are not inspected (would stop the purge part-way).

### Added — recoverable cleanup of run reports and private RTK data (menu `r`, rtk-report-cleanup-c1)

- New `copilot-s` menu option `r) Clean run reports and private RTK data
  (recoverable)`, in the session list and in the "No saved sessions found"
  menu. Its submenu previews then quarantines, lists batches, or restores a
  batch; every outcome returns to the menu and never launches Copilot.
  Existing `n`, `p`, `c`, `q` are unchanged.
- New helper `bin/copilot-report-cleanup.py` (stdlib only; installed next
  to `copilot-s`; override with `COPILOT_REPORT_CLEANUP_HELPER`):
  `preview [--include-recent]` (read-only; creates no file, folder or
  lock), `apply [--include-recent]` (exact previewed `CANDIDATE` lines on
  stdin), `list`, `restore --batch B [--yes]` (read-only plan without
  `--yes`). No purge command in that cycle (added later by
  quarantine-purge-c1, above).
- Scope: finalized per-launch run reports of ALL workspaces under
  `~/.copilot/session-reports/runs/<run-id>/` — whole folder, including the
  private `raw/` data (RTK history DB, tee/recall, Copilot usage file) —
  and, for RTK launches, the matching publication
  `~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json`. Never touches the
  global RTK database, Copilot session-state/logs/OTEL files, orphan
  publications or a custom `COPILOT_SESSION_REPORTS_DIR` (refused).
- Strict eligibility: `completed`/`copilot-error`/`interrupted` with a
  consistent state/outcome/exit code, schema v1 (integer), valid ordered
  timestamps, finalized and unchanged for 24h (explicit opt-in for recent
  runs); incomplete, report-failure, preflight-failed, unknown and malformed
  runs are always kept. Plain private bounded trees only; a system `lsof`
  must report every file/folder and the publication closed.
- Apply re-validates under the transaction → reconcile → run → capacity
  locks, then moves (atomic no-replace rename, same filesystem) the
  publication and the run folder into
  `~/.copilot/copilot-s-report-quarantine/<batch>/` with a durable
  manifest. Nothing is deleted and **no disk space is freed**.
- Restore requires unchanged quarantined data and absent original paths
  (no overwrite or merge); the run folder returns before its publication.

### Changed

- `copilot-session-report.py`: start-up reconciliation re-lists run folders
  while holding its lock before sweeping publications whose run folder is
  gone, so a run restored meanwhile keeps its publication. No change to
  records, publication schema, cadence or capacity eviction.
- Installer installs `copilot-report-cleanup.py`; the uninstaller leaves
  `~/.copilot/copilot-s-report-quarantine` untouched.

### Added — automatic per-launch RTK publication for external viewers (copilot-s-rtk-c1)

- `copilot-s --rtk` launches now publish a small numeric file per launch
  automatically to `~/.copilot/copilot-s-rtk-export/v1/runs/<run-id>.json`
  (schema `copilot-s.rtk-launch` v1; ≤ 16 KiB; `0700` folders, `0600` files,
  atomic replace after exact-contract validation; unsafe folders/files are
  refused, never repaired). One independent file per launch, so parallel,
  resumed and multi-launch sessions never overwrite each other; nothing is
  merged or summed. Run ids, record state (`active-unfinalized` /
  `finalized`), launch kind, timestamps (`published_at` distinct from
  `measured_at`), the session link with its proof label or `unknown`, and RTK
  availability/measurement kind/numeric statistics only (signed
  `total_saved`, finite `avg_savings_pct` that may be negative). No commands,
  prompts, output, paths, error text or costs. Replaces the unreleased manual
  `export-rtk` command and its fixed `v1.json` (removed; an existing file is
  ignored).
- **While a launch runs:** copilot-s starts the launch's own background
  publisher (`copilot-session-report.py rtk-publisher`, internal) right
  before Copilot and stops it (SIGTERM to its process group, ≤ 5 s, then
  SIGKILL) and reaps it right after Copilot exits, before finalization and
  on exit. It republishes about every 30 s (first after ~2 s) with at most
  one bounded `rtk gain --format json` at a time (re-validated recorded
  binary, private `RTK_*` paths only, 15 s / 1 MiB, cancellable), exits when
  its parent is no longer the launcher (`getppid`), ignores Ctrl-C meant for
  Copilot, and writes under the run lock only while the record is not
  finalized. A failed measurement leaves the previous file untouched (no
  fresher timestamp is invented). Not started for normal-mode launches or
  when the RTK start/preflight fails; the existing environment isolation,
  binary identity checks and launch cleanup are unchanged.
- **At finalization:** `finish` publishes the statistics it stored (no extra
  RTK run) while still holding the run lock; failures never change the run
  record, output or exit status.
- **Historical backfill:** every launch with a report runs one bounded,
  resumable reconciliation slice at `start` (≤ 256 run folders probed by
  metadata after a persisted cursor, ≤ 32 records loaded, ~1 s, non-waiting
  lock; never runs RTK): finalized RTK runs from the last 14 days are
  (re)published from stored records (backfill never evicts and adds files
  only below 128); publications not rewritten for 14 days, of non-publishable
  records or (default reports root) of deleted run folders are withdrawn.
- **Capacity:** at most 256 publication files, all writes serialized by a
  capacity lock. A live publisher or `finish` write that needs a new file at
  the cap evicts the oldest contract-valid finalized publication(s) (by
  `finalized_at`) from the sanitized cache; active publications are never
  evicted (all 256 active: the write fails closed). Run records and reports
  are never touched.
- Session links are stricter than run-record links: only
  `launcher-selected-resume` or `single-conversation-id-in-own-otel-file`.
  Active runs: the run's own hook log and own per-run OTEL file, read
  incrementally (no-follow, descriptor-checked, not group/world-writable,
  ≤ 64 MiB, complete lines only; an unusable file stays unusable).
  Finalized runs: the stored confirmed link with one of those bases, its
  stored evidence counters and OTEL/hook diagnostics (strict, never
  defaulted to 0) and a fresh secure hook-log scan must all agree; a stored
  `unique-new-session-dir` basis is published as `unknown` (known
  limitation). A confirmed link that becomes unknown is republished as
  `unknown`.
- Launches already running from an older copilot-s have no publisher
  (reopen or resume them through the updated launcher); they are published
  once finalized.
- Internal: the RTK gain summary parser is shared by finalization and the
  publisher (`parse_gain_summary`); `run_lock` takes an optional wait;
  finalization output is unchanged.

### Added — per-launch session reports and opt-in RTK mode (rtk-session-reports-20261118-c1)

- New `bin/copilot-session-report.py` (stdlib): every `copilot-s` launch,
  normal or RTK, gets a metadata-only run record under
  `~/.copilot/session-reports/runs/<run-id>/run.json` (0700/0600, atomic,
  per-run lock): mode, state (`incomplete` → `completed` / `copilot-error`
  / `interrupted`, or `report-failure`), exit code, wall clock, exact
  session link or `unknown` with a reason, versions, OTEL calls/tokens and
  summed model time from the launch's own OTEL file (deduplicated by
  trace/span id), an estimated cost priced once with the existing
  cache-only official snapshot and frozen, Copilot's own
  `--usage-output-file` summary (numeric fields only, shown separately), and
  an explicit `task_outcome` (`mark-outcome`; never inferred from exit 0).
  Read-only `launch`, `session` and `compare` commands (text/json) and
  `copilot-s --session-report SESSION_ID`. Task reports, their CLI and
  schema are unchanged; no historical backfill.
- `copilot-s --rtk` (per launch, never persisted): exactly RTK 1.2.0; refuses
  to launch when RTK user config/filters/trust store or an existing
  `rtk hook` Copilot configuration could alter results; clears inherited
  `RTK_*` variables and isolates RTK's database, tee, recall and pipeline
  directory in the run's private folder; loads the session-local
  `bin/copilot-s-rtk-plugin` hook via `--plugin-dir` (never installed as a
  Copilot plugin). The hook rewrites only allowlisted plain test/build
  commands to exactly `rtk <command>`, never approves anything (Copilot
  permissions unchanged), and denies an allowlisted command if RTK fails.
  RTK statistics come only from the run's own database; the value of
  avoided tokens is an explicitly approximate one-exposure estimate at
  the run's official input rate(s) (range for several models, `unknown`
  without a priced call; output growth is disclosed, never a saving).
- Token fields a span did not report are recorded as unknown with per-field
  coverage (never a measured 0); the RTK value estimate is `partial` and
  names unpriced models when some calls have no known rate; RTK-mode
  preflight also refuses hook-configuration candidates it cannot verify
  (symlinks — never followed —, non-regular, unreadable or oversized
  files); a malformed RTK hook answer denies the command; an invalid or
  deeply nested Copilot usage file is reported unavailable without
  affecting the rest of the run record.
- RTK mode also inspects (never modifies) caller `--plugin-dir` plugins and
  refuses ones that run `rtk hook` or cannot be verified; the hook denies an
  allowlisted command when RTK asserts its own permission decision; invalid
  native usage values make that summary `partial`/`unavailable`.
- RTK-mode preflight checks every file of installed plugins (bounded,
  symlink-safe, depth/size caps refuse) and the project hook configuration
  of the directories the session will actually run in (menu/`--resume=ID`
  recorded directory, `-C DIR`, launch directory); it refuses when that
  directory cannot be determined (`--worktree`, `--connect`, `--continue`,
  bare `--resume`), and normal mode then records a baseline note. Project
  checks also cover `.github/copilot/settings*.json`, every `--add-dir`
  directory, and the real path (and its git root) of symlinked directories.
- OTEL export files are now named
  `copilot-otel-<YYYYMMDD-HHMMSS>-<run-id>.jsonl` (same `copilot-otel-*`
  pattern), so concurrent launches never share a file.
- Normal launches now also pass `--usage-output-file` into the run's
  private folder unless the caller supplied one (then left untouched).
- `scripts/install.sh` installs the helper and the plugin files
  (symlink/copy, manifest-managed); `scripts/uninstall.sh` removes them,
  prunes the plugin directories only if empty, and never touches session
  reports or their private raw folders (no automatic retention/deletion).

### Changed — cost-disciplined orchestration (delivery-coordination-20261117-c1)

- `instructions/copilot-instructions.md` gains an *Efficient orchestration*
  section: the main session answers small questions and follows a single
  continuous investigation directly, may make genuinely trivial edits
  itself when the handoff costs more than the edit (main-authored code
  edits still get the independent reviewer pass), and delegates only
  substantial context or demonstrable net savings — tiny lookups, status
  checks and one-line commands are no longer pushed to the built-in `task`
  route by default. The small/casual `coder` route is now the *default*
  (not mandatory-for-everything) route; its model/effort/context are
  unchanged. Implementers get one complete bounded batch, tests run before
  one broad review of the whole batch, parallelism is limited to
  independent reads/non-overlapping writes, and only significant
  correctness/security/safety/data-integrity/regression findings consume
  fix rounds (optional polish is deferred unless requested).
- Budget exhaustion keeps the existing 1 + 3 limits per work item across
  sessions and now produces one actionable escalation with explicit choices
  (accept as-is, authorize a targeted extension, stop), then waits: no extra
  round, automatic new cycle, budget reset or reminder loop without explicit
  user authorization, and no completion claim while waiting.
- Delivery is judged by completion, cost, elapsed time and confirmed
  material defects/regressions, not agent count or self-reported quality.
  The handoff manifest gains an optional `Feature:` field and a new
  *Recording feature delivery* subsection applies to meaningful
  multi-agent features only.
- Agent role bodies (no frontmatter change) reflect batching, required vs
  optional finding triage, regression evidence, and single escalation.
  `config/agent-routing.yaml` adds a policy-only
  `task_routing.orchestration_policy` block; every model/effort/context
  mapping is unchanged.

### Added — feature delivery tracking and per-call ledger (delivery-coordination-20261117-c1)

- Ingest now also keeps an additive, versioned per-call ledger in the task
  JSON (`feature_usage`, schema v1) keyed by session id + `traceId` + the
  chat span's own `spanId`, with minimal usage data and the official price
  result frozen at ingestion (or the unpriced reason); `invoke_agent` spans
  are kept as observed invocations. Calls without stable ids are counted
  per reason, repeated keys are never ledgered twice, and usage already on
  a task is frozen as an unsplittable pre-ledger baseline (no backfill).
  Existing totals, cost, attribution and review semantics are unchanged.
- New `copilot-task-report.py record-delivery --input FILE`
  (`copilot-task-report.delivery-record` v1): started / completed /
  cancelled lifecycle per feature cycle plus confirmed material
  `defect-confirmed` / `regression-confirmed` findings with stable ids.
- New read-only `copilot-task-report.py feature-calls --task T --session-id S`
  (JSON listing of ledgered calls and invocation groups; with selectors and
  `--feature-id/--cycle-id` it prints a ready-to-save payload) and
  `record-feature-usage --input FILE`
  (`copilot-task-report.feature-usage-record` v1), which links exact call
  keys or direct-invocation groups to one started cycle after re-checking
  the previewed count and sha256.
- Reports gain a *Feature Delivery* section (only when such records exist):
  per-cycle/per-feature status, wall-clock elapsed, linked calls and their
  frozen estimate (partial/unpriced labeled), linked model-call time,
  recorded confirmed defects/regressions, and a ledger coverage table that
  reconciles with the Summary. Legacy reports are unchanged. No new
  executable is installed; the existing helper carries the commands.
- `record-delivery` and `record-feature-usage` only add to a task file that
  an ingest already created; they never create a task.
- Ingest no longer aborts on a usage attribute it cannot convert
  (NaN/Infinity, non-numeric strings, oversized values) or on non-finite
  span times. OTEL and events.jsonl line readers now skip any malformed
  line (including integers too long for Python's JSON parser, invalid
  UTF-8 and excessively nested JSON) instead of failing the whole ingest
  and leaving the cursor stuck. Newly ingested calls whose raw usage count is present but not
  a strict count (boolean, numeric string, fraction, negative, unsafe huge,
  NaN/Infinity, non-numeric) are now recorded as unpriced with the new
  reason "a usage count in telemetry is not a valid count …" instead of
  being priced from a guessed value, and are not ledgered (they appear as
  unallocated, invalid usage). The legacy token aggregates keep the previous
  lenient conversion (unconvertible values count as 0), so totals may
  include such normalized values; non-finite span times are treated as
  unknown. Missing optional attributes, the missing input/output reasons,
  and every estimate recorded before this change are unchanged (never
  repriced).

### Added — subagent model refresh from current prices (model-refresh-20261116-c1)

- New `copilot-s` menu `p) Refresh subagent models from current prices`,
  also offered when there are no saved sessions (that screen now waits for
  Enter/`n` to start a new session, `p`, or `q` instead of launching
  immediately; `p` never launches a session). It runs the new
  `bin/copilot-model-refresh.py` (`status`, `preview`, `propose`, `apply`,
  `discard`, `reset`), shows current vs proposed model · effort · context for
  ten routes (seven agents, small/casual coder, stronger reviewer, built-in
  `task`) with official per-1M rates, snapshot id/age, reasons and evidence,
  and applies only after an exact `yes` (default No), then prints the final
  effective routing and waits for Enter before returning to the menu.
- Fit comes from the new dated, sourced `config/model-role-catalog.json`
  (official Category, Task area, extended-capability and Copilot CLI client
  labels transcribed from docs.github.com on 2026-11-16; expires
  2027-02-14). Unassessed models are listed, never assigned; replacements
  also need the user's verified `available_models` list, the route's
  specific reasoning level verified for the model (source-backed catalog
  `reasoning_levels` — none shipped — or the user's dated
  `verified_capabilities`; unknown fails closed), and a ≥ 10% saving under a
  disclosed blended workload mix (Default tier, plus the above-threshold
  tier for `long_context` routes). Stale/unknown/unpriced → keep. The table
  states up front that fit is categorical, not measured or equal quality;
  user-fixable blocks end with an ACTION REQUIRED block and a config
  example (always on a first run). An existing managed override whose
  evidence becomes unavailable/stale is kept with its reason (never
  reverted or re-selected); an unpriced current model is always kept;
  catalog origins with future-dated or inconsistent review/source dates
  authorize nothing; all entered dates use the local calendar date.
- The orchestrator reads routing live via the new read-only
  `copilot-model-refresh.py status --instructions` before each toolkit
  delegation; fields set in `/subagents` print as `OMIT` (left out of the
  task call) and are never frozen into the generated instruction or
  `effective-routing.json`, which exist only while a managed override or
  user-config pin exists. Interrupted-apply journals are validated (known
  targets, matching backups inside the tool's backups folder) before any
  rollback and are never deleted when untrustworthy.
- Local-only, two-phase, journaled apply: frozen proposal digests, refusal
  on concurrent edits, backups, rollback/crash recovery. Writes managed
  `subagents.agents.<agent>` fields in `~/.copilot/settings.json` (effective
  override values with user-config pins applied; never a user-set
  `/subagents` value), a generated personal instruction
  `~/.copilot/instructions/copilot-toolkit-model-routing.instructions.md`,
  and state under `~/.copilot/model-refresh/`. Shipped routing defaults,
  agent frontmatter and tests' expected defaults are unchanged; the shipped
  instructions gain a directive to read `status --instructions` live before
  each toolkit delegation (generated table as fallback).
- `copilot-task-report.py`: new `refresh_official_pricing_now()` (explicit
  refresh: skips the 24h TTL and 1h failure backoff, reuses a snapshot under
  5 min old, otherwise the same fetcher/validation/cache); the parser now
  records optional per-model `category` / `release_status` labels
  (validated when present; older caches stay valid; never used for
  pricing).
- Installer links/copies `copilot-model-refresh.py` and the catalog;
  uninstall/update never touch model-refresh state (run
  `copilot-model-refresh.py reset --yes` before uninstalling).

### Added — opt-in cleanup of recent empty sessions (recent-empty-option-20261113-c1)

- Menu `c` now first asks whether to also include recent empty sessions
  (created or changed within the last 24h), default No. Opting in passes
  the new `copilot-session-cleanup.py --include-recent` flag to both
  `preview` and `apply`; it waives only the 24h minimum age (timestamps must
  still not be in the future) and keeps every other eligibility, activity,
  fingerprint, lock, and no-replace check. Previewed candidate lines carry
  an `include-recent` marker and `apply` refuses any preview/apply mismatch
  before moving anything; the batch manifest's `scope` records
  `include_recent`. The preview warns that a recent empty session's origin
  is unknown and that a race with a starting Copilot is accepted because
  quarantine is recoverable. The exact `yes` confirmation is unchanged;
  `list`/`restore` and existing batches are unaffected.

### Fixed — empty-session cleanup recognizes the empty session scaffold (empty-scaffold-fix-20261113-c1)

- `copilot-session-cleanup.py` (menu `c`) kept never-used sessions whose
  directory also held Copilot's empty scaffold, reporting them as "has
  session history or other files". It now also accepts exactly
  `workspace.yaml` plus the complete, untouched scaffold (empty regular
  `.workspace-fork.lock`, empty `files/` and `research/`, and
  `checkpoints/` with only the header-only `index.md`), verified through
  no-follow descriptors (owner, type, single link, exact bytes). Partial or
  modified scaffolds, symlinks, extra entries, and any history are still
  kept. The preview fingerprint now covers every scaffold item and is
  re-checked before and after the move and on restore; the open-file check
  includes the scaffold paths. Workspace-only fingerprints are unchanged,
  so existing quarantine batches still restore.

### Added — recoverable empty-session cleanup (cleanup-empty-sessions-20261113-c1)

- New `copilot-s` menu option `c) Clean up empty sessions (recoverable)`
  backed by the new stdlib helper `bin/copilot-session-cleanup.py`
  (installed next to `copilot-s`). It previews never-used sessions in the
  listed scope (only an unnamed `workspace.yaml`, older than 24h, not open
  per `lsof`, no session-list alias), asks for an exact `yes`, re-validates
  each previewed session, and moves it to `~/.copilot-s-quarantine/<batch>/`
  with a durable manifest. Nothing is deleted; `restore --batch <batch>`
  moves sessions back. Copilot's native session index is not modified.
- `~/.copilot-sessions` rewrites by `copilot-s` and the helper now take a
  shared `flock` on `~/.copilot-sessions.lock`; if it is busy the index is
  left unchanged (a delete is cancelled) instead of rewritten unlocked.

### Fixed — new-session names (session-name-20261112-c1)

- Pass prompted names through Copilot's native `--name` option at launch
  (verified on CLI 1.0.109), instead of saving only a wrapper index alias
  after exit. Preserve explicit caller naming/session-selection options
  and do not reapply the prompted name over a later in-session rename.

### Fixed — official pricing image-table compatibility

- Exclude the explicitly recognized non-chat `Image generation models`
  table from official chat pricing snapshots; image generation remains
  unsupported and unpriced. Malformed chat tables still reject the whole
  refresh. TLS verification, secure curl fallback, cache expiry, and retry
  policies are unchanged.

### Changed — model routing defaults, Auto Balance, per-agent attribution

- `copilot-s` now launches Copilot with `--model auto --auto-tier balance`
  (was the Efficiency tier); caller `--model`/`--auto-tier` options are
  still stripped so they cannot override it.
- Routing cycle `sol-medium-20261028-c1`: `coder` uses
  `gpt-6.1-sol`/`medium` (default context) for the quality-first default;
  its small/casual override moves from Luna to
  `gpt-6.1-terra`/`low` (default context). This is a conservative
  recommendation, not a local benchmark result. Artificial Analysis task
  cost figures are not Copilot bills or evidence of coding/review quality.
  `tester` uses `gpt-6.1-sol`
  (`xhigh`, default context); `test-reviewer` moves to
  `claude-sonnet-5.5` (`high`, long context) so Opus 5.5 stays reserved for
  `senior-coder` and the stronger reviewer override. `discovery` and the
  built-in `task` route stay on `gpt-6-luna` (`low`). Other routes are
  unchanged.
  Synchronized across `config/agent-routing.yaml`,
  agent frontmatter, instructions, and README.
- Task reports now attribute model calls to agents through explicit OTEL
  trace context only: a `chat` span's own `parentSpanId` naming an
  `invoke_agent` span (same trace, same ingest) whose `gen_ai.agent.name`
  identifies the agent. A new *By Agent* table shows invocation spans,
  calls, tokens, time, models, and the sum of per-call official USD
  estimates recorded at ingestion. Calls without that link stay unknown
  with a specific reason (no trace parent / parent not observed in the
  batch / parent not an agent span) and are never re-linked; nothing is
  inferred from timestamps, models, session membership, or review
  outcomes. An *Attribution data quality* list adds coverage percentages
  and per-reason hints. The attribution block moves to schema v2; a v1
  block is upgraded in place by adding an empty `attributed` bucket, and
  its earlier unknown rows are kept and labeled as pre-join history.

### Changed — visible signals for blocking user requests

- Require the preceding user-facing message to start with
  `🛑 **NEEDS YOUR APPROVAL**` before blocking approval requests, or
  `🛑 **NEEDS YOUR INPUT**` for blocking non-approval questions. Apply this
  to routing, plan/action authorization, and clarification pauses; retain
  distinct permission scopes and do not signal a request only in a final
  response. This is an instruction convention, not enforceable CLI behavior.

### Added — context metrics for new copilot-s sessions

- Sessions newly created by `copilot-s` (and identified unambiguously after
  the run) show the context % *last observed before compaction* (latest
  valid `session.compaction_start` `currentTokens / tokenLimit`, not
  live/at-exit usage) and the number of successful compactions, on exit
  before keep/rename/delete and in the session list. Cached per session in
  `copilot-s-context.json` via new `copilot-task-report.py context-update`
  / `context-list` commands. Existing sessions are never enrolled or
  scanned; the `~/.copilot-sessions` format is unchanged.

### Added — batched review handoff, attribution coverage, review records

- Default handoff order is now implement → relevant tests → one
  comprehensive reviewer pass over the whole code-and-test batch →
  test-reviewer for complex/high-risk work. `tester` is skipped when
  existing tests suffice (the built-in `task` route runs them). Stages use
  a compact handoff manifest; later fix rounds carry only the changed
  subset and finding numbers. Tests added after a coverage review need a
  retest and a focused correctness check inside the same budgets, else
  escalate. Budgets and reviewer models (Sonnet 5.5 default, Opus 5.5 for
  senior-coder/complex/high-risk/unknown) are unchanged.
  `config/agent-routing.yaml` gains `broad_review_timing` and
  `review_record_command` under `task_routing.code_review`.
- Task reports gain an *Attribution Coverage* section backed by a new
  versioned `attribution` block: usage already on a task becomes frozen
  `legacy`, new calls are `unknown` with an explicit reason (no supported
  telemetry link from model calls to agent invocations; nothing is
  inferred from timestamps or models), and buckets reconcile with the
  totals. No per-agent cost is measured; official cost estimates are
  unchanged and nothing is repriced or backfilled. An informational
  registry records invocations observed in `events.jsonl` (session id +
  `agentId`), merged idempotently, including registry-only ingests.
- New `copilot-task-report.py record-review --input <JSON-file>` command
  stores validated, orchestrator-supplied reviewer/test-reviewer round
  records (`copilot-task-report.review-record` v1) and enforces the bounded
  per-cycle review policy (no second broad review, ordered rounds ≤ 3, no
  rounds after escalation, no reset by changing session). Approval is not
  terminal: a later in-order focused round of the same cycle can check
  tests/fixes changed after it, and the report's stage outcome is the
  latest round. Budgets are per requested work item (`cycle_id`), not per
  task-report lifetime.
  Rejections exit non-zero with a stderr message; identical replays are
  no-ops. It never ingests, fetches pricing, or regenerates reports.
  Reports show recorded reviews and their exact-id registry check. Missing
  historical records are not a compliance failure.
- `ingest` now reads an existing task file strictly (as `record-review`
  does): an unreadable/non-object file or one naming another task is refused
  with a non-zero exit before any offsets or task data are written, instead
  of being silently replaced by an empty task. Markdown table cells escape
  backslashes before pipes.

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
