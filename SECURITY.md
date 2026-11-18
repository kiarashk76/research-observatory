# Security and privacy

Research Observatory is intended for a trusted operator on a controlled Linux host. It is not a public multi-tenant service. Read the README trust boundaries before connecting accounts or exposing a dashboard.

## Never commit installation state

Keep `.slack.env`, local `config.yaml`, `.env` files, credentials/keys, `runtime/`, `workspaces/`, `projects/`, `library/`, private backups and logs out of Git. The ignore file prevents common accidents; it does not remove already tracked content or scan old commits. Run `python scripts/check_public_release.py` and inspect staged files before pushing.

The public distribution uses generic configuration and synthetic fixtures. A new clone should start with no provider accounts, Slack messages, database, research content or network-access state. Authenticate and provision your own installation locally.

## Deployment

Keep the dashboard bound to loopback. For remote access, use an SSH tunnel/private network or the documented authenticated HTTPS proxy. Do not forward a public tunnel to the raw dashboard port. Dashboard access is owner-level access to the exposed workspace data/functions; no read-only guest role or organization-level tenant isolation is implemented.

Use a dedicated OS user and restrict that user's home, runtime and backups. Configure Slack's allowed-user list. The CLI needs its own credentials and network access; filesystem restrictions do not make prompt injection or credential misuse impossible. Research code and external paper/web content must be treated as untrusted inputs.

Do not disable the sandbox to work around installation failures. Use a compatible kernel, configure legitimate dependencies and report missing capabilities. Shared GPU memory reservations are scheduling estimates, not hard driver isolation.

## Vulnerability reports

Do not include secrets, live data or provider histories in public issues. Report general code/configuration problems with synthetic reproductions. Use a repository owner's private security reporting channel when available for sensitive vulnerabilities. A future repository deployment should explicitly enable and publish that channel; this source distribution does not contain a private contact address.

If a credential was published, revoke it first. Deleting a commit, removing a file, or changing repository visibility is insufficient on its own.
