## Summary

<!-- What does this PR change, and why? -->

## Checklist

- [ ] `bash -n bin/copilot-s scripts/install.sh scripts/uninstall.sh scripts/update.sh` passes
- [ ] `shellcheck -S warning` passes on changed shell scripts
- [ ] `python3 -m unittest tests.test_copilot_jira_report -v` passes
- [ ] Install/uninstall smoke-tested against a throwaway `HOME` (not my real one)
- [ ] Updated `README.md` / `docs/architecture.md` if behavior changed
- [ ] Added a `CHANGELOG.md` entry under "Unreleased"
- [ ] No personal paths, real Jira keys/session IDs, or real usage/report data included
