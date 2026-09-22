#!/usr/bin/env bash
# install.sh — install/link the copilot-cli-toolkit into a user's Copilot CLI
# setup, making this repository the source of truth for the tools.
#
# By default, files are installed as SYMLINKS pointing back into this repo
# checkout, so a `git pull` (or `scripts/update.sh`) immediately updates the
# live tools with no re-install step. Use --copy to install plain file
# copies instead (e.g. for environments where symlinks are undesirable).
#
# Usage:
#   scripts/install.sh [--copy] [--dry-run] [--prefix DIR]
#
#   --copy       Install copies instead of symlinks.
#   --dry-run    Print what would happen without changing anything.
#   --prefix DIR Override the executables' install directory
#                (default: "$HOME/.local/bin"). Must be non-empty. Relative
#                paths are canonicalized to an absolute path (resolved
#                against the current directory) before use, so the
#                manifest recorded for `scripts/uninstall.sh` is correct
#                regardless of which directory install.sh is later re-run
#                from. If you pass a `~`-prefixed path, quote it so your
#                shell doesn't mangle it, but be aware `~` is expanded by
#                your shell BEFORE install.sh ever sees it — a literally
#                quoted `--prefix '~/bin'` is treated as a literal
#                subdirectory named "~", not $HOME/bin. Use an unquoted
#                `--prefix ~/bin` or an explicit `--prefix "$HOME/bin"`.
#
# Idempotent: running this script repeatedly is safe. Any pre-existing file
# at a destination that isn't already managed by this toolkit is backed up
# (never deleted) before being replaced. The user's editable pricing config
# (model-pricing.json) is only ever copied into place if it does not already
# exist at the destination — it is never overwritten or symlinked.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE="symlink"
DRY_RUN=false
BIN_PREFIX="$HOME/.local/bin"
COPILOT_HOME="$HOME/.copilot"

STATE_DIR="$HOME/.copilot-cli-toolkit"
MANIFEST_FILE="$STATE_DIR/install-manifest.txt"
BACKUP_ROOT="$STATE_DIR/backups"

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
DIM='\033[2m'
RESET='\033[0m'

usage() {
  # Print the header comment block (everything from line 2 up to the first
  # non-comment line), rather than a brittle fixed line range that silently
  # truncates whenever the header comment grows or shrinks.
  awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --copy) MODE="copy"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --prefix)
      if [[ $# -lt 2 ]]; then
        echo "Error: --prefix requires a directory argument." >&2
        usage >&2
        exit 1
      fi
      if [[ -z "$2" ]]; then
        echo "Error: --prefix requires a non-empty directory argument." >&2
        usage >&2
        exit 1
      fi
      BIN_PREFIX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# Canonicalize a (possibly relative) --prefix to an absolute path *before*
# it's used to build any destination/manifest paths below. Without this,
# a relative --prefix recorded verbatim in the manifest would only ever
# resolve correctly if scripts/uninstall.sh (or a later scripts/install.sh
# re-run) happened to be invoked from the exact same working directory —
# silently breaking uninstall from anywhere else. This doesn't resolve
# symlinks or ".." segments (no realpath/readlink -f dependency, to stay
# portable to macOS's stock toolchain); it only guarantees the result is
# absolute.
if [[ "$BIN_PREFIX" != /* ]]; then
  BIN_PREFIX="$(pwd)/$BIN_PREFIX"
fi

log() { echo -e "$*"; }
run_note() { $DRY_RUN && log "${DIM}[dry-run]${RESET} $*" || true; }

mkdir_p() {
  local dir="$1"
  if $DRY_RUN; then
    [[ -d "$dir" ]] || run_note "mkdir -p $dir"
  else
    mkdir -p "$dir"
  fi
}

# Timestamp helper, portable across BSD/GNU date (no args needed here, but
# kept consistent with bin/copilot-s's approach for this repo's conventions).
timestamp() { date +%Y%m%d-%H%M%S; }

is_managed_path() {
  # True if $1 is listed in the install manifest (i.e. this toolkit put it
  # there), so it is safe to overwrite/replace without a backup.
  local target="$1"
  [[ -f "$MANIFEST_FILE" ]] || return 1
  grep -Fxq "$target" "$MANIFEST_FILE"
}

record_managed_path() {
  local target="$1"
  $DRY_RUN && return 0
  mkdir -p "$STATE_DIR"
  touch "$MANIFEST_FILE"
  if ! grep -Fxq "$target" "$MANIFEST_FILE"; then
    echo "$target" >> "$MANIFEST_FILE"
  fi
}

remove_stale_managed_path() {
  # remove_stale_managed_path TARGET REASON
  #
  # Safely removes a retired toolkit file (e.g. a superseded helper or a
  # deleted agent) and atomically prunes it from the install manifest —
  # but ONLY when it's clearly this toolkit's own doing: either recorded
  # in the install manifest, or a symlink pointing back into this repo
  # checkout. A file at TARGET that isn't ours (an unmanaged file, or a
  # symlink pointing somewhere else entirely) is left completely alone,
  # with a clear message explaining why.
  local target="$1" reason="$2"
  [[ -e "$target" || -L "$target" ]] || return 0

  local is_ours=false
  if is_managed_path "$target"; then
    is_ours=true
  elif [[ -L "$target" ]]; then
    local link_target
    link_target="$(readlink "$target")"
    [[ "$link_target" != /* ]] && link_target="$(dirname "$target")/$link_target"
    case "$link_target" in
      "$REPO_ROOT"/*) is_ours=true ;;
    esac
  fi

  if ! $is_ours; then
    log "  ${DIM}left in place${RESET} $target (not managed by this toolkit — remove it yourself if it's no longer needed)"
    return 0
  fi

  if $DRY_RUN; then
    run_note "remove stale toolkit-managed $target ($reason)"
    return 0
  fi

  rm -f "$target"
  if [[ -f "$MANIFEST_FILE" ]]; then
    local grep_rc=0
    grep -Fxv "$target" "$MANIFEST_FILE" > "$MANIFEST_FILE.tmp" 2>/dev/null || grep_rc=$?
    # grep's exit status distinguishes "removed a matching line" (0) and
    # "no lines matched / file now empty" (1) — both are a safe, valid
    # result to move into place — from a genuine read/write failure (>=2,
    # e.g. permission denied, disk error), where $MANIFEST_FILE.tmp could
    # be empty/truncated garbage. Only ever overwrite the real manifest
    # (atomically, via mv) in the first two cases; on a real error,
    # discard the tmp file and warn instead of risking wiping out the
    # install manifest.
    if [[ $grep_rc -le 1 ]]; then
      mv "$MANIFEST_FILE.tmp" "$MANIFEST_FILE"
    else
      rm -f "$MANIFEST_FILE.tmp"
      log "  ${YELLOW}warning:${RESET} could not update $MANIFEST_FILE after removing $target (grep exit $grep_rc) — manifest left untouched; it may still list the now-removed path"
    fi
  fi
  log "  ${YELLOW}removed${RESET} stale toolkit-managed $target ($reason)"
}

backup_if_needed() {
  # If $1 exists, is not already a symlink pointing at $2, and is not a
  # path this toolkit itself manages, move it aside into a timestamped
  # backup directory (preserving the original as-is) before we replace it.
  local dest="$1" want_target="$2"
  [[ -e "$dest" || -L "$dest" ]] || return 0

  if [[ -L "$dest" ]]; then
    local current_target
    current_target="$(readlink "$dest")"
    if [[ "$current_target" == "$want_target" ]]; then
      return 0  # already correct
    fi
  fi

  if is_managed_path "$dest"; then
    # Previously installed by this toolkit (e.g. a --copy install being
    # switched to --copy again, or a stale symlink target) — safe to
    # replace directly, no user data at risk.
    if $DRY_RUN; then
      run_note "replace previously-managed $dest"
    else
      rm -rf "$dest"
    fi
    return 0
  fi

  local ts backup_dir backup_path
  ts="$(timestamp)"
  backup_dir="$BACKUP_ROOT/$ts"
  backup_path="$backup_dir$dest"
  if $DRY_RUN; then
    run_note "back up existing $dest -> $backup_path"
  else
    mkdir -p "$(dirname "$backup_path")"
    mv "$dest" "$backup_path"
    mkdir -p "$STATE_DIR"
    echo "$ts|$dest|$backup_path" >> "$STATE_DIR/backup-manifest.txt"
    log "  ${YELLOW}backed up${RESET} pre-existing $dest -> ${DIM}$backup_path${RESET}"
  fi
}

install_one() {
  # install_one SRC DEST [executable]
  local src="$1" dest="$2" make_exec="${3:-false}"
  local want_target="$src"

  mkdir_p "$(dirname "$dest")"
  backup_if_needed "$dest" "$want_target"

  if $DRY_RUN; then
    if [[ "$MODE" == "symlink" ]]; then
      run_note "ln -sf $src $dest"
    else
      run_note "cp $src $dest"
    fi
    return 0
  fi

  if [[ "$MODE" == "symlink" ]]; then
    ln -sf "$src" "$dest"
  else
    cp -f "$src" "$dest"
    [[ "$make_exec" == "true" ]] && chmod +x "$dest"
  fi
  record_managed_path "$dest"
  log "  ${GREEN}installed${RESET} $dest -> $([[ "$MODE" == "symlink" ]] && echo "$src (symlink)" || echo "copy of $src")"
}

log "${BOLD}copilot-cli-toolkit installer${RESET} (mode: $MODE$($DRY_RUN && echo ", dry-run"))"
log "Repository: $REPO_ROOT"
log ""

# --- Executables ---
log "${BOLD}Executables${RESET} -> $BIN_PREFIX"
mkdir_p "$BIN_PREFIX"
install_one "$REPO_ROOT/bin/copilot-s" "$BIN_PREFIX/copilot-s" true
install_one "$REPO_ROOT/bin/copilot-task-report.py" "$BIN_PREFIX/copilot-task-report.py" true

# --- Remove a stale pre-2.0 copilot-jira-report.py helper, if present ---
# Only ever touches it when it's clearly this toolkit's own doing (see
# remove_stale_managed_path above). A copilot-jira-report.py that isn't
# ours is left completely alone.
remove_stale_managed_path "$BIN_PREFIX/copilot-jira-report.py" "superseded by copilot-task-report.py"

# --- Agents ---
log ""
log "${BOLD}Agents${RESET} -> $COPILOT_HOME/agents"
mkdir_p "$COPILOT_HOME/agents"
for agent_file in "$REPO_ROOT"/agents/*.agent.md; do
  name="$(basename "$agent_file")"
  install_one "$agent_file" "$COPILOT_HOME/agents/$name"
done

# --- Remove the retired fixer agent, if present ---
# The agent-pipeline redesign folded targeted-fix work directly into
# coder/senior-coder and deleted agents/fixer.agent.md from this repo, so
# it's no longer part of the *.agent.md glob above and won't get
# reinstalled. Only ever removed when it's clearly this toolkit's own
# doing (manifest-managed, or a symlink pointing back into this repo
# checkout) — see remove_stale_managed_path above. An unmanaged
# fixer.agent.md (e.g. a user's own custom agent that happens to share
# the name) is left completely alone.
remove_stale_managed_path "$COPILOT_HOME/agents/fixer.agent.md" "retired — fixes are now handled by coder/senior-coder"

# --- Orchestrator instructions ---
log ""
log "${BOLD}Orchestrator instructions${RESET} -> $COPILOT_HOME/copilot-instructions.md"
install_one "$REPO_ROOT/instructions/copilot-instructions.md" "$COPILOT_HOME/copilot-instructions.md"

# --- Legacy data migration ---
# Force any pre-2.0 ~/.copilot/jira-reports data (and its edited pricing
# config) to migrate into ~/.copilot/task-reports *now*, before the
# copy-if-absent pricing install below runs — otherwise a fresh default
# pricing file could get written first and the user's edited legacy one
# would then be skipped as "already exists". copilot-task-report.py runs
# this migration on every invocation (including `ensure-marker`), so this
# is just forcing it to happen at a convenient, deterministic point.
if $DRY_RUN; then
  run_note "python3 $REPO_ROOT/bin/copilot-task-report.py ensure-marker (also runs one-time legacy migration)"
elif command -v python3 >/dev/null 2>&1; then
  marker_warnings="$(python3 "$REPO_ROOT/bin/copilot-task-report.py" ensure-marker 2>&1 >/dev/null || true)"
  # Discard stdout (nothing useful there for this call), but never
  # silently drop stderr: a successful (exit 0) migration can still print
  # actionable warnings (e.g. an unresolved legacy-migration conflict) that
  # must reach the user, not just failures. Stay quiet only when there is
  # truly nothing to report.
  if [[ -n "$marker_warnings" ]]; then
    log "  ${YELLOW}${marker_warnings}${RESET}"
  fi
fi

# --- Pricing config: copy-only-if-absent, never overwrite user edits ---
log ""
log "${BOLD}Pricing config${RESET} -> $COPILOT_HOME/task-reports/model-pricing.json"
pricing_dest="$COPILOT_HOME/task-reports/model-pricing.json"
mkdir_p "$(dirname "$pricing_dest")"
if [[ -e "$pricing_dest" ]]; then
  log "  ${DIM}skipped${RESET} (already exists — your edits are preserved): $pricing_dest"
else
  if $DRY_RUN; then
    run_note "cp $REPO_ROOT/config/model-pricing.json $pricing_dest"
  else
    cp "$REPO_ROOT/config/model-pricing.json" "$pricing_dest"
    log "  ${GREEN}installed${RESET} $pricing_dest (initial copy — edit freely, future installs won't touch it)"
  fi
fi

log ""
if $DRY_RUN; then
  log "${YELLOW}Dry run complete — no changes were made.${RESET}"
else
  log "${GREEN}Install complete.${RESET} Make sure $BIN_PREFIX is on your PATH."
  log "Verify with: copilot-s --help  (or) /agent inside a copilot session"
fi
