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
#     copy-once-if-absent and never toolkit-managed — and the runtime
#     official pricing cache (official-pricing-cache.json,
#     official-pricing-refresh-state.json) in the same directory.
#   - Copilot session state (~/.copilot/session-state, ~/.copilot-sessions)
#   - Quarantined empty sessions (~/.copilot-s-quarantine) — restore them
#     with copilot-session-cleanup.py restore --batch <batch> before
#     uninstalling if you still want them back in session-state.
#   - Subagent model-refresh state (~/.copilot/model-refresh: confirmed
#     overrides, your user-config.json pins/availability list, backups) and
#     what an applied refresh wrote outside it (the /subagents values in
#     ~/.copilot/settings.json and the generated
#     ~/.copilot/instructions/copilot-toolkit-model-routing.instructions.md).
#     Run `copilot-model-refresh.py reset --yes` BEFORE uninstalling to
#     remove the tool's own overrides safely (your own pins are kept).
#   - Per-launch session reports (~/.copilot/session-reports or
#     $COPILOT_SESSION_REPORTS_DIR), including each launch's private raw/
#     folder (RTK's own history DB/tee/recall and Copilot usage files).
#   - Any other user data.
#
# The session-local RTK hook plugin files (copilot-s-rtk-plugin/ next to
# copilot-s) are manifest-managed and removed like any other tool file; its
# directories are then removed only if they are left empty.
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
rtk_plugin_roots=()
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  case "$path" in
    */copilot-s-rtk-plugin/*)
      rtk_plugin_roots+=("${path%%/copilot-s-rtk-plugin/*}/copilot-s-rtk-plugin")
      ;;
  esac
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

# Remove the RTK hook plugin's directories only if they are now empty
# (rmdir never deletes anything a user put there). Skipped when backups
# were restored into them.
if [[ ${#rtk_plugin_roots[@]} -gt 0 ]] && ! $RESTORE; then
  for root in "${rtk_plugin_roots[@]}"; do
    for dir in "$root/hooks" "$root/.claude-plugin" "$root"; do
      [[ -d "$dir" && ! -L "$dir" ]] || continue
      if $DRY_RUN; then
        log "${DIM}[dry-run]${RESET} rmdir $dir (only if empty)"
      elif rmdir "$dir" 2>/dev/null; then
        log "  ${GREEN}removed${RESET} empty directory $dir"
      fi
    done
  done
fi

log ""
if $DRY_RUN; then
  log "${YELLOW}Dry run complete — no changes were made.${RESET}"
else
  log "${GREEN}Uninstall complete.${RESET} Removed $removed_count toolkit-managed path(s)."
  rm -f "$MANIFEST_FILE"
  log "Note: Task reports, task state, session state, per-launch session reports (incl. their private raw folders), quarantined sessions, and your pricing config were left untouched."
  if [[ -f "$HOME/.copilot/model-refresh/state.json" ]]; then
    log "${YELLOW}Note:${RESET} subagent model-refresh state remains in $HOME/.copilot/model-refresh. If you applied overrides, restore them from the checkout with: python3 bin/copilot-model-refresh.py reset --yes"
  fi
  if ! $RESTORE; then
    log "Backups of any pre-existing files (if created during install) remain at: $STATE_DIR/backups/"
  fi
fi
