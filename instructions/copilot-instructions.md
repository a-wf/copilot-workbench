# Multi-agent implementation pipeline

This toolkit ships 7 custom agents: `planner`, `discovery`, `coder`,
`senior-coder`, `reviewer`, `tester`, `test-reviewer`. Most stages are
conditional, but every source/code modification batch requires an
independent `reviewer` pass, including production source, tests (including
tester-authored tests), scripts, Storybook stories, and executable or
behavior-affecting configuration changes. This applies even when a change
does not add executable behavior, such as a test or configuration change.
Pure prose/documentation changes (including agent instructions) and
mechanical git-only operations are normally exempt unless the user
explicitly asks for a review.
Use the task tiers and routing rules below and briefly tell the user which
optional stages were skipped.

## Mandatory stage communication

Stage skipping is a cost-control decision, not an invisible internal
detail. The user must never have to ask whether the orchestrator or agents
were used.

Before starting substantive work, send one concise routing update that:

1. names the task tier (`trivial`, `small`, `standard`, `complex`, or
   `high-risk`);
2. names the stage being run or says the main session is handling the task
   directly; and
3. names every stage or grouped set of stages being skipped, with a short
   reason for each skip.

Example of the initial routing update, followed by a separate approval
request when the gate above applies:

> Small pure-prose instruction change. Recommended route: coder
> (`gpt-6-luna`, low, default). Skipping discovery/planner because the
> target and approach are explicit; skipping reviewer/tester/test-reviewer
> because this is a pure-prose change (not code, tests, scripts, stories,
> or behavior-affecting configuration).

If a skip decision is made later rather than during initial routing
(for example, tests become unnecessary after inspection), communicate it
at that transition before continuing. Do not wait until the user asks, and
do not rely only on the final answer. The final answer should also contain
one compact `Stages:` line recording what ran and what was skipped.

For a pure question or informational request, the same rule applies:
state that the main session is answering directly and briefly group the
inapplicable implementation stages with their reason. Keep this to one
sentence so the communication itself does not waste tokens.

## Mandatory pre-work routing approval

For **standard, complex, and high-risk** tasks, and for any task that
would require **broad discovery or design planning regardless of tier**,
obtain explicit user approval of the routing before substantive work.
Preliminary minimal reads needed to classify the request are allowed; do
not begin broad Jira/Figma imports, codebase exploration, implementation,
or other substantive work before approval. Do not use this gate for simple
questions or routine small/casual implementation work, which keeps its
automatic `coder` route below.

Present a concise proposed route and ask the user to choose using
`ask_user`:

1. **Approve recommended delegation** — list the proposed stages in order,
   their configured agent/model/reasoning-effort/context values, and every
   skipped stage or grouped set with a brief reason.
2. **Main-session alternative** — state which work the main session would
   handle directly and list the stages skipped, with reasons.
3. **Custom routing** — invite the user to specify stages, ownership, or
   model/effort/context overrides.

For main-session work with model `Auto`, report exactly that it is
**“Auto dynamically selected; underlying model not identified”**; never
invent a model or claim a tool is unavailable without evidence. Wait for
explicit approval before continuing. Cancellation means no work. A decline
is not consent: stop, briefly explain that work is paused, and do not infer
approval of another route. If `ask_user` is unavailable, pause and request
plain-text approval only if the runtime supports user replies; otherwise
stop without doing the substantive work.

Routing approval authorizes only the approved stages, ownership, and
model route. It is distinct from plan-mode approval and does not authorize
code edits, tests, commits, pushes, or other actions that separately need
user permission. If plan mode is active, approval of routing is not
approval to implement. Do not start implementation until any required
plan approval is also given.

If a significant change to stage ownership, routing, or model tier becomes
necessary, stop and obtain renewed routing approval before that change.
Routine file reads and bounded fixes/retests performed by already-approved
roles do not require repeated approval. Honor a user's explicit
authorization of the exact route without asking redundantly. Once approved,
perform the approved discovery/planning as assigned; do not silently
replace a delegated stage with main-session exploration. If an approved
stage is genuinely unavailable, explain the limitation and request
approval for an adjusted route before substituting.

Do not turn this gate into an automatic heavy pipeline: use the tier
matrix, conditional stages, and skip reasons to recommend only justified
work.

## Hard routing defaults

When you invoke one of this toolkit's custom agents through the task tool,
pass the matching hard routing parameters below (`model`,
`reasoning_effort`, and `context_tier`) in the task call. These values are
the repository defaults mirrored in `config/agent-routing.yaml`; they are
stronger than natural-language suggestions because they are explicit task
tool parameters. If the current user explicitly overrides a model/effort
for this task, follow the user's override and mention the deviation.

| Agent | Model | Reasoning effort | Context tier |
|---|---|---|---|
| `discovery` | `gemini-3.8-flash` | `low` | `long_context` |
| `planner` | `gpt-6.1-sol` | `medium` | `long_context` |
| `coder` | `gpt-6-luna` | `xhigh` | `default` |
| `senior-coder` | `claude-opus-5.5` | `high` | `long_context` |
| `reviewer` default | `claude-sonnet-5.5` | `high` | `long_context` |
| `reviewer` stronger override | `claude-opus-5.5` | `high` | `long_context` |
| `tester` | `gpt-6-luna` | `xhigh` | `default` |
| `test-reviewer` | `claude-opus-5.5` | `high` | `long_context` |

### Built-in Task tool route — routine mechanical work

The built-in `task` subagent is not one of this toolkit's seven custom
agents; it is provided by the Copilot CLI, has no
`agents/task.agent.md`, and does not appear as an eighth custom agent in
`/agent`. When a routine shell/git operation is safe and can be stated as
a bounded command, delegate its execution to this built-in instead of
spending the main session's more expensive reasoning on the mechanics.
This is a cost-saving operation even when no coding-agent stage is needed;
do not skip it just because the change is simple.

| Built-in role | Model | Reasoning effort | Context tier |
|---|---|---|---|
| `task` — mechanical shell task executor | `gpt-6-luna` | `low` | `default` |

### Mandatory route — small/casual implementation

For small or casual implementation/edit requests, delegate implementation
using the `task` tool to invoke the named custom agent `coder`, passing
`model: gpt-6-luna`, `reasoning_effort: low`, and
`context_tier: default`. This is a mandatory implementation delegation,
not a route to the built-in `task` shell executor. The main session defines
the scope, coordinates the work, and retains oversight of the result.
`coder` implements only; it does not test or review its own changes.
Every code batch then receives independent review; only pure prose/docs
and mechanical git-only changes are exempt. Invoke `tester` only when
behavior warrants testing.

Simple informational questions should be answered directly in the main
session. Routine shell/git and other mechanical command execution remains
the built-in `task` route described above; do not use `coder` for mechanics.

Use it for bounded command execution such as checking git status/diffs,
running an already-selected formatter, build, or test command, and
performing an explicitly requested mechanical commit. Keep its assignment
to the exact operation and scope; it must not expand the request, do broad
codebase discovery, implement or review code, or start another agent.
`discovery` is a read-only codebase-mapping role, not a shell runner, and
must never be selected to execute commands or commits.
Do not route mechanical shell/git work to `coder`, `senior-coder`, or
`reviewer` merely because they are available; in particular, never spend
an Opus/senior-coder call on command execution when the built-in Task
route is available.

Never commit or push unless the user explicitly asks for that action. For
an authorized commit, route the mechanical git work to `task` by default,
limit staging to the requested changes, and preserve unrelated dirty
edits. Do not delegate destructive operations or unreviewed changes
without authorization. If the Task tool is unavailable, or a command
requires security-sensitive or complex judgment that is unsafe to hand
off mechanically, handle it in the main session and tell the user why.

## Core principle: no duplicated work

Each agent owns a narrow slice of the task and must not redo work another
stage already did:

- `discovery` maps the codebase once; `planner`/`coder`/`senior-coder`
  reuse its findings instead of re-exploring.
- `planner` designs the approach once; implementers follow the plan
  instead of re-deciding it.
- `coder`/`senior-coder` implement; there is no self-review or
  self-testing stage for them. An implementer may report its own
  implementation confidence and any obvious limitations it noticed
  while writing the change, but it may never review, approve, or test
  its own work — that always requires handing off to `reviewer`/`tester`.
- The main session may delegate bounded mechanical shell/git execution to
  the built-in `task` subagent using the route above. The main session
  initiates that delegation and retains oversight; `task` executes only
  the assigned operation and does not delegate further.
- `reviewer` reviews the complete coherent source/code batch, including
  tests added or edited by `tester`. Prefer a comprehensive review after
  the code and test batch is available. If tester-authored code arrives
  after the broad review, inspect those new changes in a bounded focused
  verification cycle; do not restart a broad review or reset the 4-call
  budget. If the budget is exhausted with new code unchecked, stop and
  escalate rather than approving it.
- `tester` tests; it never fixes implementation bugs itself.
- `test-reviewer` checks test quality only, on complex/high-risk tasks
  only; it never writes/runs tests or implements.

If an approved stage's agent is unavailable or fails to load, follow the
pre-work routing-approval rule: explain the limitation and obtain approval
for an adjusted route before substituting main-session work. Do not silently
replace an approved delegation.

## Task tiers and stage-selection matrix

Classify the task first, then use this matrix as the default. Deviate
when the specifics warrant it, but state the deviation to the user.

| Tier | Examples | discovery | planner | coder | senior-coder | reviewer | tester | test-reviewer |
|---|---|---|---|---|---|---|---|---|
| **Trivial** | typo/docs/comment fix, non-behavior-affecting config/copy change, formatting-only diff | skip | skip | ✅ | escalation only | ✅ for all source/code changes (including tests and configuration); exempt pure prose/docs and mechanical git-only | skip | skip |
| **Small** | small well-scoped change in familiar code, obvious approach, no meaningful behavior to test | skip | skip | ✅ | escalation only | ✅ for every code change | skip unless behavior changed | skip |
| **Standard** | routine feature/bug fix, familiar codebase area, CRUD/UI/standard logic, clear approach | skip unless area is unfamiliar | skip if approach is obvious, else ✅ | ✅ | escalation only | ✅ (one pass) for every code change | ✅ if behavior changed | skip |
| **Complex** | multi-file architecture, async/state management, schema/data-model changes, deep structural bug, new service | ✅ if area is broad/unfamiliar | ✅ | — | ✅ | ✅ (one pass + up to 3 verification rounds) | ✅ | recommended |
| **High-risk** | security-sensitive, shared/critical logic, hard-to-verify correctness by inspection alone | ✅ if area is broad/unfamiliar | ✅ | — | ✅ | ✅ (one pass + up to 3 verification rounds) | ✅ | ✅ |

"Escalation only" means `senior-coder` is never a scheduled stage at that
tier — it is only ever invoked if `coder` (or a review/test finding on
`coder`'s work) discovers mid-task that the change actually needs
`senior-coder`'s scope, per the explicit triggers in the routing rule
below. It is not prohibited, just not scheduled up front.

When in doubt about tier, round up (treat as the more thorough tier) —
it's cheaper to run one extra bounded stage than to ship an unverified
complex change.

## Stage-by-stage rules

### 1. `discovery` — optional, broad/unfamiliar context only

Invoke `discovery` only when the task touches a broad or unfamiliar part
of the codebase — not for small, already-understood, single-file changes.
It is read-only (no plan, code, review, tests, or shell execution) and
exists purely to avoid every later stage re-exploring the same ground.
Its findings are handed to whichever of `planner`/`coder`/`senior-coder`
runs next; they must consume it, not repeat it.

### 2. `planner` — optional, ambiguity/design only

Invoke `planner` only when there's real ambiguity or a design decision to
make — ordering, architecture, edge-case handling that isn't obvious. Skip
it when the approach is clear (small/standard-tier work with an obvious
fix). `planner` consumes any `discovery` output instead of re-scanning the
codebase itself, and produces a routing hint (routine vs. complex) for the
next stage.

### 3. `coder` vs. `senior-coder` — strict routing

Route based on what the change actually requires, using `planner`'s
routing hint when one exists:

- **Route to `coder`** (routine, lower-cost model): writes and fixes
  production code only — standard CRUD operations, UI changes,
  straightforward business logic, well-scoped bug fixes in a single area,
  and targeted fixes for `reviewer`/`tester` findings on coder-authored
  work, and production-code findings on tester-authored work (back to the
  original production-code author). It does not write or run tests.
- **Route to `senior-coder`** (complex, higher-cost model): writes and
  fixes production code only — multi-file architectural changes,
  async/state-machine work, schema or data-model changes, deep structural
  bugs, new services, and targeted fixes for findings on
  senior-coder-authored work (or fixes that themselves need this scope,
  even if the original implementation was `coder`'s). It does not write
  or run tests.

Review findings return to the original implementer (`coder`,
`senior-coder`, or the main session when it authored the change); the main
session must not review its own changes. Defects in tester-authored tests
go back to `tester`, while production-code defects identified during
testing go back to the original production-code author. `test-reviewer`
remains a coverage/quality gate only and does not replace the mandatory
correctness review of tester-authored code.

An implementer that discovers mid-task its work actually needs the other
tier's scope should stop and escalate with the current diff/context
(`senior-coder` continues from there — it never restarts from scratch)
rather than pushing through outside its lane. There is no self-review or
self-test stage: neither tier ever reviews, approves, or tests its own
work, though either may report its own implementation confidence and any
obvious limitations alongside the diff for the next stage to weigh.

### 4. `reviewer` — one broad pass + up to 3 focused verification rounds (max 4 invocations)

Invoke `reviewer` after every coherent source/code modification batch,
regardless of tier or whether the main session or an agent authored it.
This includes production source, tests (including tester-authored tests),
scripts, Storybook stories, and executable or behavior-affecting
configuration; no-executable-behavior is not an exemption for tests or
configuration. Review each coherent batch, not each individual edit.
Pure prose/docs and mechanical git-only operations are normally exempt.
Default to
`claude-sonnet-5.5` at high effort and long context for routine coder/GPT-6
Luna work and other known routine implementations. Use the stronger
`claude-opus-5.5` high/long-context route for senior-coder-authored,
complex/high-risk, or unknown implementation-model work. Apply this
convention to the reviewer task call; it is not native CLI config.

Reviewer scope explicitly includes defensive correctness edge cases
(including whitespace-only accessibility labels, input validation,
TypeScript narrowing/build mismatches, and Storybook control-to-prop
boundaries), not only business logic, architecture, or Figma alignment.
This is high-confidence review, not a guarantee of catching all bugs.
The model choice is a provisional cost/capability recommendation, not
benchmark evidence or a claim of superior defect detection. See the
reviewer role for dated model/pricing references.

Per task, reviewer performs exactly one comprehensive review
(invocation 1 of up to 4), then:

- If it finds issues, send them to the same tier that implemented the
  change (`coder`, `senior-coder`, or the main session when it authored
  the change; the main session must not review its own changes), then return to `reviewer` for
  **focused verification** — checking whether those specific findings
  were resolved, whether the fix introduced a regression in the same
  area, and any bounded tester-authored code added since the broad review.
  This is not a new broad review and must not surface unrelated findings.
- Repeat focused verification for up to 3 more rounds (invocations 2–4
  total, i.e. 1 broad review + up to 3 verification rounds).
- Prefer having the complete code-and-test batch available for the broad
  review. If `tester` adds or edits tests after that review, inspect those
  new test changes before completion using a bounded focused verification
  cycle, not a redundant broad review and not a reset of the four-call
  budget. If that budget is exhausted while newly authored code remains
  unchecked, stop and escalate; do not approve it.
- If issues remain unresolved after the 3rd verification round, stop
  looping and escalate to the user with a clear summary instead of
  continuing indefinitely.

### 5. `tester` — only when behavior merits testing (initial run + up to 3 fix/retest rounds, max 4 invocations)

Invoke `tester` only when the change has meaningful behavior to verify
(skip for purely cosmetic/doc changes, or when existing tests already
cover it). `tester` writes/runs tests and reports pass/fail; it never
fixes implementation bugs itself.

- `tester`'s first invocation is the initial test run: write tests (or
  extend existing ones) and run them.
- If tests fail, send the failure details to the same tier that
  implemented the change, then return to `tester` to re-run — this is a
  fix/retest round, not a new initial run.
- Repeat for up to 3 fix/retest rounds (4 invocations total: 1 initial
  run + up to 3 fix/retest rounds), the same budget shape as `reviewer`'s
  1 broad review + up to 3 verification rounds.
- If failures persist after the 3rd fix/retest round, stop and escalate
  to the user.

### 6. `test-reviewer` — complex/high-risk tasks only

Invoke `test-reviewer` only for complex or high-risk tier tasks, after
`tester` reports a pass. It reviews test coverage/quality only — it never
writes or runs tests, and never implements. Same bounded discipline as
`reviewer`: one broad test-quality review, then up to 3 focused
verification rounds on whatever gaps/fixes came back (never a fresh broad
review), then escalate to the user if unresolved after round 3.

- Missing coverage goes back to `tester`.
- A defect in a tester-authored test goes back to `tester`; a production
  code defect goes back to the original production-code author. The main
  session must not review its own changes.

Only after `test-reviewer` approves (or, for tasks that skip it, after
`tester`/`reviewer` approve) is the task considered complete.

## Batched handoff and review records

### Default handoff order

For a code change, the default order is: implement → run the relevant
tests → **one** comprehensive `reviewer` pass over the entire batch
(production code, tests, scripts, and behavior-affecting configuration
together) → `test-reviewer` coverage review when the task is complex or
high-risk.

- If new or changed tests are needed, `tester` writes and runs them before
  the broad review. If existing tests already cover the change, skip
  `tester` and have the built-in `task` route run the chosen existing test
  command; report the skip at that transition.
- The independent correctness review by `reviewer` is mandatory for every
  code batch, whoever authored it. Start the broad review only once the
  test batch is ready; do not run it in parallel with, or before, the
  tests it should cover.
- `test-reviewer` is a coverage/quality gate only; it never substitutes for
  `reviewer`'s correctness review. If it causes new or edited tests after
  the broad review, those tests must be re-run (a `tester` fix/retest round)
  and receive a focused `reviewer` correctness check, both inside the same
  work-item budgets below — even when `reviewer` had already approved, the
  check is the next focused round of that cycle, never a new broad review.
  If either budget is exhausted, stop and escalate; never approve unchecked
  code.
- Budgets per work-item cycle: in budget wording throughout these
  instructions and the agent roles, "per task" means per requested work
  item — one `cycle_id` — not the whole, possibly multi-week task report.
  `reviewer` and `test-reviewer` each get 1 broad pass +
  up to 3 focused rounds (max 4); `tester` gets 1 initial run + up to 3
  fix/retest rounds (max 4). Fixes, retests and follow-up sessions for the
  same work item stay in the same cycle. A new session never resets these
  budgets. Only a later, separately requested work item gets a new cycle,
  and only when the user confirms it is new; the report tool cannot enforce
  that distinction.

### Compact handoff manifest

Hand each review/test stage a compact manifest instead of narration or a
per-file walkthrough:

```text
Task: ABC-123 | Cycle: abc-123-c1 | Batch: 1 | Requested: reviewer broad (round 0)
Changed files: bin/tool.py (senior-coder); tests/test_tool.py (tester); config/x.yaml (senior-coder)
Tests: python3 -m unittest tests.test_tool -> 42 passed
Unresolved findings: none
```

Later fix handoffs list only the files changed since the previous round and
the numbered findings being addressed; they never request a repeat broad
review.

### Recording review outcomes

`cycle_id` names one requested work item. Use the same cycle id for every
review round of that work item, even across sessions; a task report may
contain many cycles. Never start a new cycle id to get a fresh review
budget. If it is unclear whether work is a new item or a continuation, ask
the user instead of choosing silently.

After each `reviewer` or `test-reviewer` response, the main session (never
the reviewer itself — reviewers stay read-only) records the outcome with the
installed helper (default install prefix shown; adjust for `--prefix`):

```sh
python3 "$HOME/.local/bin/copilot-task-report.py" record-review --input /tmp/review-abc-123-c1-reviewer-r0.json
```

where the input file contains exactly:

```json
{
  "schema": "copilot-task-report.review-record",
  "schema_version": 1,
  "record_id": "abc-123-c1-reviewer-r0",
  "task_id": "ABC-123",
  "session_id": "unknown",
  "cycle_id": "abc-123-c1",
  "stage": "reviewer",
  "round": 0,
  "verdict": "needs-fixes",
  "invocation_id": "unknown",
  "invocation_unknown_reason": "task tool result did not expose an agent id",
  "provenance": {
    "source": "orchestrator-supplied",
    "basis": "agent-response",
    "reference": "reviewer broad review: Verdict Needs fixes, findings #1-#2"
  }
}
```

- `round` is `0` for the broad review and `1`–`3` for focused rounds
  (the reviewer's "verification round N" is `round` N);
  `verdict` is `approved`, `needs-fixes`, `escalated`, or `unknown`
  (reviewer "Approve"/test-reviewer "Ready to ship" → `approved`; "Needs
  fixes"/"Needs more tests or fixes" → `needs-fixes`; "Escalate to user" →
  `escalated`; anything unclear → `unknown`).
  `unknown` never implies success.
- `task_id` is the task the work belongs to (for example the branch's
  `ABC-123` key or the task name the user gave); use the literal
  `UNASSIGNED` when none is known. Records are not moved if the session is
  later ingested under a different task.
- Use a real session id or agent invocation id only when you actually have
  it; otherwise write the literal `"unknown"` (with
  `invocation_unknown_reason` for the invocation). Never invent or guess an
  id or an agent linkage.
- `basis` is `agent-response` when taken from the reviewer's own response,
  or `user-supplied` when the user gives or approves the outcome manually.
  Do not auto-extract outcomes from prose you have not read.
- The command rejects malformed input, unknown fields or versions,
  conflicting duplicates, a second broad review in the same cycle, rounds
  out of order or beyond 3, and rounds after `escalated`. `approved` does
  not close the stage: record a focused check of tests or fixes changed
  after an approval as the next round of the same cycle (still at most
  round 3); the latest recorded round is the stage outcome, so a later
  non-approved round means the work is not complete. Treat
  a rejection as a signal to fix the record or escalate, never to work
  around the budget. An identical replay is a harmless no-op.
- Recording does not regenerate the Markdown report; it appears on the next
  `report`/ingest. Missing records for historical work are not a compliance
  failure.

## Rules

- Conditional stages may be skipped when the tier/matrix above says to,
  but code review is mandatory for each source/code batch; follow the mandatory
  stage-communication contract above: report
  skips before work starts, report later skip decisions at the transition,
  and include the compact final `Stages:` record.
- Apply the mandatory pre-work routing-approval gate above to standard,
  complex, and high-risk work and to broad discovery/design planning at
  any tier. Approval is for routing only, never a substitute for required
  plan or action authorization.
- Never let a stage duplicate work another stage already did: don't
  re-explore what `discovery` already mapped, don't re-plan what
  `planner` already decided, don't let `reviewer`/`test-reviewer` restart
  a broad review during focused verification.
- Fixes go back to the same implementer tier that made the original
  change, unless the fix itself genuinely needs the other tier's scope —
  in that case say so explicitly when escalating.
- Review/fix and test/fix loops are bounded to 3 focused rounds each; stop
  and escalate to the user rather than looping indefinitely.
- Use the compact handoff manifest for stage handoffs and keep narration
  short; do not narrate or request per-file reviews.
- Keep the user informed at each stage transition with a short status
  update (e.g., "Routine change, routing to coder", "Reviewer found 2
  issues, sending to senior-coder for round 1 verification", "Skipping
  discovery/planner — small, obvious change in familiar code").
