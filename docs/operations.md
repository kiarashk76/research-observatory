# Operating your installation

## Services and restart behavior

Use a dedicated Linux user and the same activated Python environment for the daemon and dashboard. `python labd.py` starts the scheduler/Slack connection; `python lab.py dashboard` starts the independent local dashboard. Keep them under an operator-controlled process supervisor or separate `screen`/`tmux` sessions.

There is a daemon lock. Starting a second daemon should fail instead of double-scheduling work. The dashboard has its own process/port. Browser refresh updates the UI/saved records; it does not restart Python services or guarantee a new provider usage check.

Python service changes need a controlled restart. Do not indiscriminately kill Python/Claude processes: some may be running jobs. The daemon preserves durable process identities/logs for recovery/adoption on restart, but an upgrade should still be scheduled around active work. Frontend-only changes need rebuilding the bundled assets and refreshing the browser.

No boot service, SSH account, public hostname, secret, or service-manager configuration from another installation is included. Install and configure those for your own host.

## Account profiles

Each named account has a separate authentication directory. Authenticate it with its exact `CLAUDE_CONFIG_DIR`. CLI credential refresh and platform metadata checks are different operations: a usage check does not guarantee a login renewal.

Use `python lab.py subscription --refresh` to inspect fresh provider metadata without waking a model. Authentication and utilization errors should remain visible; never infer successful sign-in merely from the presence of a file. A running session keeps the profile it launched with. Saved application histories/memory can support continuity across profiles without sharing refresh tokens in project artifacts.

Rotate/revoke credentials using the provider/Slack interfaces if exposed. Removing a file or making a repository private does not undo public credential exposure. Restore backups carefully because old refresh credentials or access-service state may no longer be valid.

## Operator commands

`python lab.py --help` and subcommand `--help` describe the current CLI. Useful operator commands include `doctor`, `doctor --slack`, `init`, `status`, `schedule`, `subscription --refresh`, and `dashboard --port 8766`.

`checkpoint`, `work status`, `job start/status/approve/stop` have authorization/context requirements; do not assume an arbitrary shell becomes a specialist workspace by changing one environment variable. Inspect code/help before making lifecycle changes.

Project pause gates new work; it does not necessarily stop running CLI sessions or compute processes. Thread closure is broader than cancelling an individual message. Slack message deletion is currently not a cancellation operation. Keep an explicit operator procedure for stopping an experiment when necessary, and preserve its outputs/checkpoints.

## Backups

A complete private backup includes the SQLite database and referenced project/workspace/library/artifact files. Stop services for a consistent full filesystem snapshot, or use SQLite's backup API plus a coordinated file snapshot. Do not copy an active database without understanding WAL consistency. Database records alone do not contain every PDF, figure, checkpoint or run result.

Treat auth directories, Slack tokens, dashboard credentials, session logs, memory and server-access state as secrets or sensitive data. Encrypt and restrict private backups, define retention, and keep them outside the public repository. Do not back up runtime auth by committing it to Git.

## Diagnostics

- Read launch holds before changing limits. Separate subscription holds from global/project time reservations, documentation focus, dependencies and compute scarcity.
- Inspect job receipts and original logs rather than assuming a nonzero command or a gray label proves a scheduler bug.
- An agent may return `partial`, `blocked`, `needs_resources` or `waiting_jobs`; these have different recovery needs.
- Use the current workspace and `$TMPDIR`. A denial on shared environment/project evidence may be the intended isolation boundary.
- Match helper tool types: researchers are read-only, reviewers have Bash but no explicit Write/Edit, implementers can produce assigned files. Narrative helper findings are collected automatically from final responses.
- Large PDFs/texts may need source-search and bounded page/offset reads. A text extraction is not visual figure verification.
- CLI tool names/schemas/hooks vary by version. Test updates in an isolated installation, not by repeatedly replaying a real task.
- Slack socket reconnect warnings are not sufficient evidence that a message was lost or successfully delivered. Inspect saved requests/outbox and Slack state before any replay to avoid duplicate work.

Session logs contain private model/provider and research content. Share only redacted diagnostics when reporting issues. Never post `.slack.env`, auth files, debug traces, database snapshots or whole workspaces to a public issue.
