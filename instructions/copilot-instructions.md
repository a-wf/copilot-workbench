# Multi-agent implementation pipeline

For any non-trivial coding task (new feature, bug fix, or refactor that involves
writing or changing code), always delegate work through the following custom
agents, in this exact order. Do not skip stages, and do not attempt to do a
stage's work yourself in the main session — invoke the corresponding agent.

## Pipeline

1. **planner** — Always start here for any implementation task. Produce an
   ordered plan before any code is written. If the task is trivial (a one-line
   change, a typo fix, a pure question with no code change), you may skip
   straight to `coder` or answer directly — use judgment, but prefer running
   the full pipeline whenever the task touches more than one file or has any
   design ambiguity.
2. **coder** — Implement the plan from `planner`.
3. **reviewer** — Review the `coder`'s changes. If the reviewer finds issues:
   - Send the findings to **fixer**, then return to **reviewer** to confirm
     the fixes resolve them. Repeat until the reviewer approves.
4. **tester** — Once the reviewer approves, write and run tests. If tests
   fail:
   - Send the failure details to **fixer**, then return to **tester** to
     re-run. Repeat until tests pass.
5. **test-reviewer** — Once tests pass, review test coverage and trust the
   results. If issues are found:
   - Send back to **tester** (missing coverage) or **fixer** (implementation
     bug), then re-run the relevant prior stage. Repeat until the
     test-reviewer approves.
6. Only after **test-reviewer** approves is the task considered complete.
   Summarize the full pipeline outcome to the user (what was planned, built,
   reviewed, tested, and confirmed).

## Cost-saving: skip unnecessary stages

Not every task needs the full six-stage pipeline — running every stage on a
trivial change wastes tokens/money for no real benefit. Use judgment to skip
stages that clearly aren't needed, but be conservative: when in doubt, keep
the stage rather than skip it.

- **Skip freely** for small, low-risk, easily-verified work: typo/docs/comment
  fixes, one-line config or copy changes, renames, formatting-only diffs,
  trivial additions with no logic, or direct questions with no code change.
  For these, it's fine to go straight to `coder` (or answer directly) and
  skip `planner`, `reviewer`, `tester`, and `test-reviewer` entirely.
- **Skip selectively** for small-but-real changes: e.g. skip `planner` if the
  approach is obvious and unambiguous; skip `tester`/`test-reviewer` if the
  change has no meaningful behavior to test (or existing tests already cover
  it); skip `reviewer` only if the change is small enough that the coder's
  own self-check is clearly sufficient.
- **Do not skip** for anything non-trivial: multi-file changes, new features,
  bug fixes with non-obvious root causes, anything touching shared/critical
  logic, security-sensitive code, or changes where correctness is hard to
  verify by inspection alone. When skipping any stage, briefly tell the user
  which stage(s) were skipped and why, so it's never silent.

## Rules

- Always run stages in order; never let `coder` skip `planner`, or `tester`
  skip `reviewer`, etc.
- Loops between reviewer/fixer or tester/test-reviewer/fixer are expected and
  fine — keep looping until that stage approves, don't move forward
  prematurely.
- If a stage's agent is unavailable or fails to load, fall back to performing
  that stage's responsibilities directly in the main session, but state
  clearly that you did so instead of silently skipping it.
- Keep the user informed at each stage transition with a short status update
  (e.g., "Plan approved, handing off to coder", "Reviewer found 2 issues,
  sending to fixer").
