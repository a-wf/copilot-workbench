#!/usr/bin/env bash
# test_update.sh — hermetic smoke tests for scripts/update.sh.
#
# Exercises the actual `git pull --ff-only` + re-run-installer flow against
# a local bare "remote" repo (a plain directory under $TMPDIR, addressed by
# filesystem path — no network access, no GitHub involved), so this is
# meaningfully different from (and a complement to) `bash -n` / shellcheck:
# it proves the argument-parsing and git-pull behavior actually works, not
# just that the script parses.
#
# Deliberately run with the *system* `/bin/bash` (not `env bash`) wherever
# this script itself is invoked with it, so that on macOS this exercises
# the stock Bash 3.2 `/bin/bash` that update.sh must remain compatible
# with (see the `--skip-pull`/empty-array regression test below). CI wires
# this up explicitly; see .github/workflows/ci.yml.
#
# Usage: bash tests/test_update.sh   (or: /bin/bash tests/test_update.sh)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# NOTE: update.sh resolves its own REPO_ROOT from ${BASH_SOURCE[0]} (its own
# on-disk location), not from $PWD. So to actually exercise the git-pull
# flow against our hermetic fixture, tests must invoke the *copy* of
# update.sh that lives inside $CHECKOUT (set up below) — invoking
# "$REPO_ROOT/scripts/update.sh" here would just operate on this real
# checkout instead of the fixture. UPDATE_SH is (re)pointed at the
# checkout's copy once the fixture exists.

pass_count=0
fail_count=0

ok() {
  pass_count=$((pass_count + 1))
  echo "  ok - $*"
}

fail() {
  fail_count=$((fail_count + 1))
  echo "  NOT OK - $*" >&2
}

assert_eq() {
  local expected="$1" actual="$2" msg="$3"
  if [[ "$expected" == "$actual" ]]; then
    ok "$msg"
  else
    fail "$msg (expected [$expected], got [$actual])"
  fi
}

assert_true() {
  # $1: 0 if the caller's condition was true, non-zero otherwise. Callers
  # capture a `[[ ... ]]` condition's status themselves (via `if`, so this
  # script's own `set -e` isn't tripped by an expected-false condition)
  # rather than passing a condition string to `eval`, which would both
  # obscure variable usage from shellcheck and be unsafe with unsanitized
  # input.
  local status="$1" msg="$2"
  if [[ "$status" -eq 0 ]]; then
    ok "$msg"
  else
    fail "$msg"
  fi
}

# --- Fixture: a local, filesystem-only bare "remote" + working checkout.
# No network access anywhere in this file.
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

REMOTE="$WORK/remote.git"
SEED="$WORK/seed"
CHECKOUT="$WORK/checkout"

git init --bare -q "$REMOTE"
git init -q "$SEED"
# Copy this repo's working tree content into the seed working tree so the
# fixture's install.sh/update.sh are the real scripts under test, not
# stand-ins. Copied directly from the working tree (not `git archive
# HEAD`) so this works whether or not REPO_ROOT currently has any commits.
(cd "$REPO_ROOT" && tar -cf - --exclude=.git --exclude=node_modules --exclude=__pycache__ .) | (cd "$SEED" && tar -xf -)
git -C "$SEED" add -A
git -C "$SEED" -c user.email=test@example.com -c user.name=test commit -q -m "seed"
git -C "$SEED" branch -M main
git -C "$SEED" remote add origin "$REMOTE"
git -C "$SEED" push -q origin main

git clone -q "$REMOTE" "$CHECKOUT" 2>/dev/null
# Robust against the ambient `init.defaultBranch` config: if it's already
# "main" (or "main" happens to be whatever HEAD the bare remote/clone
# settled on), the clone may already have checked out a local "main"
# branch — in which case `checkout -b main` would fail with "a branch
# named 'main' already exists" and abort this script under `set -e`. If
# it's anything else, no local "main" branch exists yet and needs
# creating. `checkout -B` handles both: it creates "main" if absent, or
# resets/re-points it (and switches to it) if already checked out,
# either way tracking origin/main.
git -C "$CHECKOUT" checkout -q -B main --track origin/main

UPDATE_SH="$CHECKOUT/scripts/update.sh"

echo "== test_update.sh: hermetic fixture at $WORK =="

# --- Test 1: no args, real (local, non-network) `git pull --ff-only`,
# followed by a real (default symlink-mode) install into a throwaway HOME.
# This is the "no args" case referenced by the final review: it must not
# error under `set -u` even though update.sh builds an empty install_args
# array with nothing to pass through.
TESTHOME1="$WORK/home1"
mkdir -p "$TESTHOME1"
# Capture output/status via the `if`-condition form (not a bare
# `out1=$(...); rc1=$?` sequence): under `set -e`, a failing command
# substitution assigned directly to a variable aborts the whole test
# script immediately (before rc1 is even read), losing diagnostics for
# every remaining test. Using the assignment as an `if` condition is
# exempt from `set -e` and lets us capture both $out1 and $rc1 for a
# meaningful (non-aborting) assertion below.
if out1="$(cd "$CHECKOUT" && HOME="$TESTHOME1" bash "$UPDATE_SH" 2>&1)"; then
  rc1=0
else
  rc1=$?
fi
assert_eq "0" "$rc1" "no-args update.sh exits 0"
if [[ -L "$TESTHOME1/.local/bin/copilot-s" ]]; then st=0; else st=1; fi
assert_true "$st" "no-args update.sh installs copilot-s as a symlink"
if [[ "$out1" == *"Update complete."* ]]; then st=0; else st=1; fi
assert_true "$st" "no-args update.sh prints completion banner"

# --- Test 1b: same as above, but explicitly under macOS's stock
# /bin/bash 3.2 semantics for empty-array expansion under `set -u`
# (regression test for the bug fixed in update.sh).
TESTHOME1B="$WORK/home1b"
mkdir -p "$TESTHOME1B"
OUT1B="$WORK/test_update_1b.out"
if (cd "$CHECKOUT" && HOME="$TESTHOME1B" /bin/bash "$UPDATE_SH" --skip-pull >"$OUT1B" 2>&1); then
  st=0
else
  st=1
  cat "$OUT1B" >&2
fi
assert_true "$st" "no-args (--skip-pull) update.sh exits 0 under /bin/bash"
if [[ -L "$TESTHOME1B/.local/bin/copilot-s" ]]; then st=0; else st=1; fi
assert_true "$st" "no-args (--skip-pull) install succeeds under /bin/bash"

# --- Test 2: direct passthrough of installer args (no `--` separator),
# using --skip-pull to keep this test hermetic/fast. Must not silently
# drop --copy.
TESTHOME2="$WORK/home2"
mkdir -p "$TESTHOME2"
OUT2="$WORK/test_update_2.out"
if HOME="$TESTHOME2" bash "$UPDATE_SH" --skip-pull --copy >"$OUT2" 2>&1; then
  st=0
else
  st=1
  cat "$OUT2" >&2
fi
assert_true "$st" "direct passthrough --copy: update.sh exits 0"
if [[ -f "$TESTHOME2/.local/bin/copilot-s" && ! -L "$TESTHOME2/.local/bin/copilot-s" ]]; then st=0; else st=1; fi
assert_true "$st" "direct passthrough '--copy' actually installs a plain copy (not a symlink)"

# --- Test 3: legacy `--` separator still works and forwards args verbatim.
TESTHOME3="$WORK/home3"
mkdir -p "$TESTHOME3"
OUT3="$WORK/test_update_3.out"
if HOME="$TESTHOME3" bash "$UPDATE_SH" --skip-pull -- --copy >"$OUT3" 2>&1; then
  st=0
else
  st=1
  cat "$OUT3" >&2
fi
assert_true "$st" "legacy '--' separator: update.sh exits 0"
if [[ -f "$TESTHOME3/.local/bin/copilot-s" && ! -L "$TESTHOME3/.local/bin/copilot-s" ]]; then st=0; else st=1; fi
assert_true "$st" "legacy '-- --copy' still installs a plain copy"

# --- Test 4: update.sh's own --help exits 0, prints its own usage, and
# has no side effects (no git pull, no install, no files under HOME).
TESTHOME4="$WORK/home4"
mkdir -p "$TESTHOME4"
if out4="$(HOME="$TESTHOME4" bash "$UPDATE_SH" --help 2>&1)"; then
  rc4=0
else
  rc4=$?
fi
assert_eq "0" "$rc4" "update.sh --help exits 0"
if [[ "$out4" == *"update.sh"* && "$out4" == *"--skip-pull"* ]]; then st=0; else st=1; fi
assert_true "$st" "update.sh --help documents itself (mentions --skip-pull)"
if [[ -z "$(ls -A "$TESTHOME4" 2>/dev/null)" ]]; then st=0; else st=1; fi
assert_true "$st" "update.sh --help has no filesystem side effects under HOME"

# --- Test 5: error path — an unknown installer arg passed through must
# surface install.sh's own error/help, not be silently swallowed, and
# must exit non-zero.
TESTHOME5="$WORK/home5"
mkdir -p "$TESTHOME5"
set +e
out5="$(HOME="$TESTHOME5" bash "$UPDATE_SH" --skip-pull --this-flag-does-not-exist 2>&1)"
rc5=$?
set -e
if [[ "$rc5" -ne 0 ]]; then st=0; else st=1; fi
assert_true "$st" "unknown passthrough arg exits non-zero"
if [[ "$out5" == *"Unknown argument"* ]]; then st=0; else st=1; fi
assert_true "$st" "unknown passthrough arg surfaces install.sh's error (not silently ignored)"

echo ""
echo "== test_update.sh: $pass_count passed, $fail_count failed =="
[[ "$fail_count" -eq 0 ]]
