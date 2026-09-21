# Security Policy

## Reporting a vulnerability

If you find a security issue in this toolkit (e.g. a path/injection issue
in `scripts/install.sh` or `scripts/uninstall.sh`, an unsafe file-permission
or symlink handling bug, or a way for `bin/copilot-task-report.py` to read
or write outside its intended directories), please report it privately:

- Open a [GitHub Security Advisory](../../security/advisories/new) on this
  repository, or
- Email the maintainers listed in the repository's contact information.

Please do not open a public issue for security reports until a fix is
available.

## Scope

This toolkit runs entirely locally and does not transmit data anywhere on
its own. In scope for security reports:

- `scripts/install.sh` / `scripts/uninstall.sh` / `scripts/update.sh` —
  file/symlink handling, backup/restore logic, permission handling.
- `bin/copilot-s` — session-state file handling, shell injection via
  branch names/session names/task ID input.
- `bin/copilot-task-report.py` — file locking, atomic writes, path
  handling, JSON parsing of untrusted/malformed telemetry files.

Out of scope: the upstream `copilot` CLI itself (report those to GitHub),
and the general security posture of your own `~/.copilot` directory
permissions.

## Supported versions

This is a personal-workflow toolkit distributed via git; only the latest
commit on the default branch is supported. There is no separate long-term
support branch.
