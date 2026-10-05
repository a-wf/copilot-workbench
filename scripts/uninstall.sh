#!/usr/bin/env bash
# uninstall.sh — remove copilot-cli-toolkit-managed files/symlinks.
#
# Only removes paths this toolkit's installer actually created (tracked in
# ~/.copilot-cli-toolkit/install-manifest.txt). It NEVER deletes:
#   - Task usage reports (~/Desktop/CopilotTaskReports or
#     $COPILOT_TASK_REPORTS_DIR)
#   - Task/session ingest state (~/.copilot/task-reports/tasks,
#     session-state.json, install-marker.json)
#   - The user's pricing config (~/.copilot/task-reports/model-pricing.json)
#     and company request-pricing config
#     (~/.copilot/task-reports/request-pricing.json) — both are
#     copy-once-if-absent and never toolkit-managed.
#   - Copilot session state (~/.copilot/session-state, ~/.copilot-sessions)
#   - Any other user data.
#
# Two retired paths are handled by scripts/install.sh instead of here:
#   - A stale pre-2.0 copilot-jira-report.py helper.
#   - The retired agents/fixer.agent.md agent (removed from this repo when
#     the fixer role was folded into coder/senior-coder).
# Both cleanups are install/update-time migration concerns, not uninstall
# concerns: they run when scripts/install.sh (or scripts/update.sh, which
# calls it) re-installs from a newer checkout and finds a toolkit-managed
# path on disk that no longer has a corresponding source file. This script
# needs no Jira- or fixer-specific logic of its own — it simply removes
# whatever the current manifest lists.
#
# Usage:
#   scripts/uninstall.sh [--dry-run] [--restore-backups]
#
#   --dry-run          Print what would happen without changing anything.
#   --restore-backups  After removing toolkit files, restore the most
#                       recent backed-up pre-existing file at each path
#                       (from ~/.copilot-cli-toolkit/backups/), if any.
set -euo pipefail

DRY_RUN=false
RESTORE=false
STATE_DIR="$HOME/.copilot-cli-toolkit"
MANIFEST_FILE="$STATE_DIR/install-manifest.txt"
BACKUP_MANIFEST="$STATE_DIR/backup-manifest.txt"

BOLD='\033[1m'
GREEN='\033[32m'
YELLOW='\033[33m'
DIM='\033[2m'
RESET='\033[0m'

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --restore-backups) RESTORE=true; shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { echo -e "$*"; }

if [[ ! -f "$MANIFEST_FILE" ]]; then
  log "${YELLOW}No install manifest found at $MANIFEST_FILE — nothing to uninstall.${RESET}"
  exit 0
fi

log "${BOLD}copilot-cli-toolkit uninstaller${RESET}$($DRY_RUN && echo " (dry-run)")"
log ""

# Most-recent-backup lookup for a given original destination path.
latest_backup_for() {
  local dest="$1"
  [[ -f "$BACKUP_MANIFEST" ]] || return 1
  # backup-manifest.txt lines are "timestamp|original_dest|backup_path",
  # appended in chronological order — last matching line wins.
  awk -F'|' -v d="$dest" '$2 == d { line = $0 } END { if (line) print line }' "$BACKUP_MANIFEST"
}

removed_count=0
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  if [[ -L "$path" || -e "$path" ]]; then
    if $DRY_RUN; then
      log "${DIM}[dry-run]${RESET} rm $path"
    else
      rm -f "$path"
      log "  ${GREEN}removed${RESET} $path"
    fi
    removed_count=$((removed_count + 1))
  fi

  if $RESTORE; then
    match="$(latest_backup_for "$path" || true)"
    if [[ -n "$match" ]]; then
      backup_path="$(echo "$match" | awk -F'|' '{print $3}')"
      if [[ -e "$backup_path" ]]; then
        if $DRY_RUN; then
          log "${DIM}[dry-run]${RESET} restore backup $backup_path -> $path"
        else
          mkdir -p "$(dirname "$path")"
          cp -a "$backup_path" "$path"
          log "  ${GREEN}restored${RESET} $path from backup ($backup_path)"
        fi
      fi
    fi
  fi
done < "$MANIFEST_FILE"

log ""
if $DRY_RUN; then
  log "${YELLOW}Dry run complete — no changes were made.${RESET}"
else
  log "${GREEN}Uninstall complete.${RESET} Removed $removed_count toolkit-managed path(s)."
  rm -f "$MANIFEST_FILE"
  log "Note: Task reports, task state, session state, and your pricing config were left untouched."
  if ! $RESTORE; then
    log "Backups of any pre-existing files (if created during install) remain at: $STATE_DIR/backups/"
  fi
fi
