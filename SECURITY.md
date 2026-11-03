# Security reporting

For vulnerabilities, use this repository's GitHub **Security → Report a
vulnerability** when private vulnerability reporting is enabled. The maintainer
must enable it before announcing this as a supported private channel. If it is
unavailable, open a generic issue asking for a private contact without including
exploit details, credentials, production data or sensitive URLs.

Include affected revision, impact, minimal synthetic reproduction, expected versus
actual behavior, and the relevant authorization boundary. We do not promise a
response SLA or enterprise support contract.

This is an actively developed reference implementation. Production use requires
deployment-specific threat modelling, maintained service distributions, trusted
TLS, identity/email configuration, capacity tests, monitoring and restore drills.
The local sandbox uses development checks and must not be exposed to the Internet.

If a credential is exposed, revoke/rotate it first. Deleting a file or adding it
to `.gitignore` does not remove copies in Git history, forks or previous downloads.
Never post a credential in an issue to ask whether it is sensitive.
