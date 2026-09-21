#!/usr/bin/env bash
# update.sh — pull the latest toolkit changes and re-run the installer.
#
# Usage:
#   scripts/update.sh [install.sh args...]
#   scripts/update.sh [--skip-pull] [install.sh args...]
#   scripts/update.sh [--] [install.sh args...]
#   scripts/update.sh -h | --help
#
# Any arguments are passed straight through to install.sh, e.g.:
#   scripts/update.sh --copy --prefix /opt/bin
#
# --skip-pull   Skip `git pull` and just re-run the installer with the
#               remaining args. Useful if you already pulled, if you're
#               offline, or (primarily) for hermetic test fixtures that
#               must not touch the network.
# -h, --help    Show this help for update.sh itself and exit — does not
#               run git pull or install.sh. For install.sh's own flags,
#               see install.sh --help, or forward `--help` unmodified
#               with the legacy `--` separator below.
#
# A leading `--` separator (from earlier versions of this script) is still
# accepted: everything after it is forwarded to install.sh completely
# unparsed (so `scripts/update.sh -- --help` shows install.sh's help
# instead of this script's), but it is never required — arguments are
# passed straight through either way, and --copy/--prefix/etc. are never
# silently dropped.
#
# Uses `git pull --ff-only` so it never creates merge commits or silently
# resolves conflicts — if the local checkout has diverged or has
# uncommitted changes that conflict, it fails loudly instead of guessing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BOLD='\033[1m'
GREEN='\033[32m'
RED='\033[31m'
RESET='\033[0m'

usage() {
  # Print the header comment block (everything from line 2 up to the first
  # non-comment line) — see scripts/install.sh for the same convention.
  awk 'NR==1{next} /^#/{sub(/^# ?/, ""); print; next} {exit}' "$0"
}

skip_pull=false
install_args=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --)
      # Legacy separator: forward everything after it verbatim, with no
      # further interpretation (so --skip-pull/-h after it are passed
      # through to install.sh rather than acted on here).
      shift
      install_args+=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --skip-pull)
      skip_pull=true
      shift
      ;;
    *)
      install_args+=("$1")
      shift
      ;;
  esac
done

cd "$REPO_ROOT"

if $skip_pull; then
  echo -e "${BOLD}Skipping git pull${RESET} (--skip-pull) in $REPO_ROOT"
else
  if [[ ! -d .git ]]; then
    echo -e "${RED}Error: $REPO_ROOT is not a git checkout — cannot update via git pull.${RESET}" >&2
    echo "Re-clone the repository instead, or fetch a new release manually." >&2
    exit 1
  fi

  echo -e "${BOLD}Updating copilot-cli-toolkit${RESET} in $REPO_ROOT"
  if ! git pull --ff-only; then
    echo -e "${RED}git pull --ff-only failed.${RESET} Your local checkout may have diverged or have" >&2
    echo "uncommitted changes. Resolve manually (e.g. git status / git stash), then re-run." >&2
    exit 1
  fi
fi

echo ""
echo -e "${BOLD}Re-running installer${RESET}"
# Bash-3.2-compatible expansion of a possibly-empty array under `set -u`:
# plain "${install_args[@]}" throws "unbound variable" on macOS's stock
# bash 3.2 when the array has zero elements, even though the array itself
# is declared. The "${arr[@]+"${arr[@]}"}" idiom expands to nothing when
# the array is empty/unset and to the normal word-split list otherwise.
"$SCRIPT_DIR/install.sh" "${install_args[@]+"${install_args[@]}"}"
echo -e "${GREEN}Update complete.${RESET}"
