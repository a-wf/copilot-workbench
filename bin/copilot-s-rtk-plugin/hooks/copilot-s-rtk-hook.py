#!/usr/bin/env python3
"""copilot-s RTK mode PreToolUse hook (session-local, loaded only by
`copilot-s --rtk` through `--plugin-dir`).

Reads Copilot's Claude-compatible PreToolUse payload for the Bash tool
(`session_id`, `tool_name`, `tool_input.command`) on stdin and either:

  * prints nothing (exit 0): the command runs UNCHANGED. This is the answer
    for every command outside the conservative allowlist below and for every
    command RTK itself does not rewrite to exactly `rtk <original command>`;
  * prints `hookSpecificOutput.updatedInput` with the command rewritten to
    `rtk <original command>` (all other tool_input fields preserved). No
    `permissionDecision` is ever emitted for a rewrite, so Copilot's own
    permission rules are evaluated, unchanged, against the rewritten command;
  * prints a `permissionDecision: deny` with a clear reason when an
    allowlisted command needed RTK but RTK is missing, failed, timed out,
    answered unparseably or in an unexpected shape, or asserted its own
    permission decision (fail closed — never a silent fallback to the raw
    command, and never an approval).

The allowlist is deliberately tiny: simple test/build invocations only,
one plain command with plain arguments. Anything with shell composition,
redirects, pipes, globs, quotes, substitutions, variables, environment
assignments, paths as the program, or exact-output/review tools (git, diff,
grep, cat, ...) is never touched. No eval, no shell, no dynamic code: the
command string is only tokenized on whitespace after rejecting every shell
metacharacter.

Per invocation one content-free evidence line (timestamp, session id,
decision and reason code — never the command) is appended to
$COPILOT_S_RUN_DIR/hook-events.jsonl (0600) for the per-launch report.
"""
import json
import os
import re
import stat
import subprocess
import sys
import time

STDIN_CAP = 1024 * 1024
RTK_TIMEOUT_SECONDS = 8
RTK_OUTPUT_CAP = 256 * 1024
MAX_COMMAND_LEN = 1000
MAX_TOKENS = 64

# Only the RTK variables copilot-s set for this launch may reach `rtk`.
RTK_ENV_ALLOWED = frozenset((
    "RTK_DB_PATH", "RTK_TEE_DIR", "RTK_RECALL_DB", "RTK_CONFIG_DIR", "RTK_TELEMETRY_DISABLED",
))

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# Any of these anywhere in the command means "not a plain command": shell
# composition (; & |), redirects (< >), substitution/variables ($ `),
# grouping/subshells ( ) { }, globs (* ? [ ]), home/history expansion (~ !),
# comments (#), quoting/escapes (' " \) and line breaks.
SHELL_META_RE = re.compile(r"[;&|<>$`(){}*?\[\]~!#'\"\\\n\r\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:=,@%+-]+$")

# program -> allowed first argument (None = any plain arguments).
# For "npm"/"pnpm" the allowed form is `run <script>`.
ALLOWLIST = {
    "cargo": frozenset(("test", "build", "check")),
    "go": frozenset(("test", "build")),
    "make": frozenset(("test", "check", "build")),
    "mvn": frozenset(("test", "compile")),
    "dotnet": frozenset(("build",)),
    "playwright": frozenset(("test",)),
    "bun": frozenset(("test",)),
    "pytest": None,
    "jest": None,
    "tsc": None,
    "ctest": None,
    "phpunit": None,
    "rspec": None,
}
RUN_SCRIPT_PROGRAMS = {"npm": frozenset(("test", "build")), "pnpm": frozenset(("test", "build"))}


def classify(command):
    """Return (eligible, reason_code) for a raw Bash command string."""
    if not isinstance(command, str) or not command.strip():
        return False, "empty"
    if len(command) > MAX_COMMAND_LEN:
        return False, "too-long"
    if SHELL_META_RE.search(command):
        return False, "shell-syntax"
    tokens = command.split(" ")
    tokens = [t for t in tokens if t]
    if not tokens or len(tokens) > MAX_TOKENS:
        return False, "token-count"
    if not all(SAFE_TOKEN_RE.match(t) for t in tokens):
        return False, "unsafe-token"
    program = tokens[0]
    if "=" in program or "/" in program:
        return False, "not-plain-program"
    if program in RUN_SCRIPT_PROGRAMS:
        if len(tokens) >= 3 and tokens[1] == "run" and tokens[2] in RUN_SCRIPT_PROGRAMS[program]:
            return True, "allowlisted"
        return False, "not-allowlisted"
    if program not in ALLOWLIST:
        return False, "not-allowlisted"
    allowed = ALLOWLIST[program]
    if allowed is None:
        return True, "allowlisted"
    if len(tokens) >= 2 and tokens[1] in allowed:
        return True, "allowlisted"
    return False, "not-allowlisted"


def _run_dir():
    path = os.environ.get("COPILOT_S_RUN_DIR") or ""
    if not path or not os.path.isabs(path):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return None
    return path


def log_event(session_id, decision, reason):
    """Append one content-free evidence line. Never raises."""
    run_dir = _run_dir()
    if run_dir is None:
        return
    sid = session_id if isinstance(session_id, str) and SESSION_ID_RE.match(session_id) else None
    line = json.dumps({"v": 1, "ts": round(time.time(), 3), "sid": sid, "decision": decision,
                       "reason": reason}, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.path.join(run_dir, "hook-events.jsonl"), flags, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


def deny(session_id, reason_code, message):
    log_event(session_id, "denied", reason_code)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "copilot-s RTK mode: %s The command was NOT run. Fix RTK or exit and "
                                    "relaunch without --rtk." % message,
    }}))
    sys.stdout.flush()
    return 0


def rtk_rewrite(rtk_bin, command):
    """Ask `rtk hook copilot` (fed a minimal synthetic payload: no session id,
    cwd or other fields are forwarded). Returns ("rewrite", new_command),
    ("none", None) or ("error", reason_code)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RTK_") or k in RTK_ENV_ALLOWED}
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": command}})
    try:
        proc = subprocess.run([rtk_bin, "hook", "copilot"], input=payload.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                              timeout=RTK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "error", "rtk-timeout"
    except OSError:
        return "error", "rtk-not-runnable"
    if proc.returncode != 0:
        return "error", "rtk-exit-nonzero"
    out = proc.stdout[:RTK_OUTPUT_CAP + 1]
    if len(out) > RTK_OUTPUT_CAP:
        return "error", "rtk-output-too-large"
    text = out.decode("utf-8", errors="replace").strip()
    if not text:
        return "none", None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return "error", "rtk-output-unparseable"
    # Any non-empty answer must be RTK's documented shape: an object with a
    # hookSpecificOutput object. Anything else (arrays, scalars, missing or
    # mistyped fields) is malformed and fails closed, never "unchanged".
    hso = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    if not isinstance(hso, dict):
        return "error", "rtk-output-malformed"
    if "permissionDecision" in hso:
        # RTK only asserts a decision (allow or deny) from user permission
        # rules, which copilot-s refuses to run with — so one here is
        # unexpected. Never forward it (an "allow" would bypass the host's
        # permissions) and never drop it (a "deny" would run the command
        # unfiltered): fail closed.
        return "error", "rtk-upstream-decision"
    updated = hso.get("updatedInput")
    new_cmd = updated.get("command") if isinstance(updated, dict) else None
    if not isinstance(new_cmd, str):
        return "error", "rtk-output-malformed"
    if new_cmd == "rtk " + command:
        return "rewrite", new_cmd
    return "none", None


def main():
    raw = sys.stdin.buffer.read(STDIN_CAP + 1)
    if len(raw) > STDIN_CAP:
        # Oversized payload: nothing to rewrite safely; leave it unchanged.
        log_event(None, "unchanged", "payload-too-large")
        return 0
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        log_event(None, "unchanged", "payload-unparseable")
        return 0
    if not isinstance(data, dict):
        log_event(None, "unchanged", "payload-unparseable")
        return 0
    session_id = data.get("session_id")
    tool_input = data.get("tool_input")
    if data.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        log_event(session_id, "unchanged", "not-bash")
        return 0
    command = tool_input.get("command")
    eligible, reason = classify(command)
    if not eligible:
        log_event(session_id, "unchanged", reason)
        return 0

    rtk_bin = os.environ.get("COPILOT_S_RTK_BIN") or ""
    if not os.path.isabs(rtk_bin) or not os.path.isfile(rtk_bin) or not os.access(rtk_bin, os.X_OK):
        return deny(session_id, "rtk-missing", "the RTK binary recorded at launch is missing or not executable.")
    kind, value = rtk_rewrite(rtk_bin, command)
    if kind == "error":
        return deny(session_id, value, "`rtk hook copilot` failed (%s)." % value)
    if kind == "none":
        log_event(session_id, "unchanged", "rtk-no-exact-rewrite")
        return 0
    log_event(session_id, "rewritten", "rtk-prefix")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": dict(tool_input, command=value),
    }}))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail closed with a clear reason, never a traceback
        sys.exit(deny(None, "hook-internal-error", "the copilot-s RTK hook hit an internal error."))
