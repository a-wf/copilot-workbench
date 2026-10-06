# Multi-agent implementation pipeline

This toolkit ships 7 custom agents: `planner`, `discovery`, `coder`,
`senior-coder`, `reviewer`, `tester`, `test-reviewer`. Every stage is
**optional** — the goal is to spend the fewest tokens/dollars that still
get the task done correctly, not to run every stage on every task. Use the
task tiers and routing rules below to decide what to invoke, invoke only
those agents, and briefly tell the user which stages you skipped and why
so it's never silent.

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

> Small instruction-only change. Recommended route: coder
> (`gpt-6-luna`, low, default). Skipping discovery/planner because the
> target and approach are explicit; skipping reviewer/tester/test-reviewer
> because there is no executable behavior.

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
| `reviewer` | `claude-opus-5.5` | `high` | `long_context` |
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
Depending on risk and behavior, the main session may additionally invoke
`reviewer` and/or `tester` when warranted, but neither is required for
every small implementation.

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
- `reviewer` does one broad review, then only narrow, bounded
  verification of what came back — never a second broad pass.
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
| **Trivial** | typo/docs/comment fix, one-line config/copy change, formatting-only diff | skip | skip | ✅ | escalation only | skip | skip | skip |
| **Small** | small well-scoped change in familiar code, obvious approach, no meaningful behavior to test | skip | skip | ✅ | escalation only | skip (implementer may note its own confidence/limitations, but does not review/approve) | skip unless behavior changed | skip |
| **Standard** | routine feature/bug fix, familiar codebase area, CRUD/UI/standard logic, clear approach | skip unless area is unfamiliar | skip if approach is obvious, else ✅ | ✅ | escalation only | ✅ (one pass) if risk/behavior warrants it or it isn't directly verifiable by inspection; otherwise skip | ✅ if behavior changed | skip |
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
  work. It does not write or run tests.
- **Route to `senior-coder`** (complex, higher-cost model): writes and
  fixes production code only — multi-file architectural changes,
  async/state-machine work, schema or data-model changes, deep structural
  bugs, new services, and targeted fixes for findings on
  senior-coder-authored work (or fixes that themselves need this scope,
  even if the original implementation was `coder`'s). It does not write
  or run tests.

An implementer that discovers mid-task its work actually needs the other
tier's scope should stop and escalate with the current diff/context
(`senior-coder` continues from there — it never restarts from scratch)
rather than pushing through outside its lane. There is no self-review or
self-test stage: neither tier ever reviews, approves, or tests its own
work, though either may report its own implementation confidence and any
obvious limitations alongside the diff for the next stage to weigh.

### 4. `reviewer` — one broad pass + up to 3 focused verification rounds (max 4 invocations)

Invoke `reviewer` for standard tier and above once the implementer is
done. For small/trivial tiers there is no reviewer stage at all — the
implementer may note its own confidence/limitations, but that is not a
review or approval, and `reviewer` is simply skipped, not replaced.

`reviewer` uses `claude-opus-5` by explicit user selection: it's the most
expensive model in the pipeline, so it's deliberately gated rather than
run out of habit. It is skipped entirely for trivial/small tiers, and for
standard-tier work it only runs when risk or behavior genuinely warrants
it — skip it for standard-tier changes that are directly verifiable by
inspection. Complex and high-risk tiers always get a review pass; that
cost is justified there.

When it does run, `reviewer` performs exactly one comprehensive review
(invocation 1 of up to 4), then:

- If it finds issues, send them to the same tier that implemented the
  change (`coder` or `senior-coder`), then return to `reviewer` for
  **focused verification** — checking only whether those specific
  findings were resolved and whether the fix introduced a regression in
  the same area. This is not a new broad review and must not surface new,
  unrelated findings.
- Repeat focused verification for up to 3 more rounds (invocations 2–4
  total, i.e. 1 broad review + up to 3 verification rounds).
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
- An actual implementation bug goes back to whichever tier implemented the
  change.

Only after `test-reviewer` approves (or, for tasks that skip it, after
`tester`/`reviewer` approve) is the task considered complete.

## Rules

- Every stage is optional — skip freely when the tier/matrix above says
  to, but follow the mandatory stage-communication contract above: report
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
- Keep the user informed at each stage transition with a short status
  update (e.g., "Routine change, routing to coder", "Reviewer found 2
  issues, sending to senior-coder for round 1 verification", "Skipping
  discovery/planner — small, obvious change in familiar code").
