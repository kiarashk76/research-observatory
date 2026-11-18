# Initial public source release

This is a source-only distribution for a new operator's own installation. Start with the README, copy the example configuration locally, and supply your own accounts and Slack app.

Included: platform/backend/dashboard/editor source and static assets, prompts, generic setup templates, synthetic tests, MIT platform license, and third-party notices. Excluded: deployed configuration, credentials, auth/session history, databases, research content, logs, private audit records, remote-access state and private Git history.

Validation of this release copy:

- 267 offline platform tests passed, including enabled browser/PDF and filesystem/process checks.
- 18 JavaScript editor/layout/rendering/dashboard tests passed.
- Frontend dependencies installed from the lockfile and assets rebuilt.
- Local documentation links checked.
- Tracked files checked for private paths/data directories, recognizable credential patterns, deployment/person identifiers and the source installation's known credential fingerprints; no matches found.
- Fresh repository with a neutral project commit identity; no inherited installation Git history.

These checks are scoped guardrails, not a guarantee that future changes cannot introduce sensitive information. Review staged changes, configuration defaults, host requirements and the security documentation before installing or publishing modifications. Real model/provider integration checks remain explicitly opt-in and may consume your allowance.
