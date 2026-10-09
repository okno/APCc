# Security policy

APCc is an administrative control interface for equipment that supplies power. Restrict access to trusted operators on a management network or VPN. The bundled installer permits only loopback clients until you explicitly configure an allowlist.

## Supported version

Security fixes target the latest published 3.x release. There is no promised response-time SLA. Review releases before deployment and keep Python, Apache, OpenSSL, NUT, and the host operating system updated.

## Reporting a vulnerability

Use this repository's **Security → Advisories → Report a vulnerability** flow if private vulnerability reporting is available. Include the affected version, a minimal synthetic reproduction, expected and actual behavior, and the impact. Never include real credentials, private keys, addresses, serial numbers, or operational logs without redaction.

If that private reporting option is unavailable, open an issue asking for a private reporting channel. Do not publish exploit details, secrets, or production device information in the issue.

## Security model

- HTTPS termination and an explicit network allowlist are enforced by Apache. The application listener stays on loopback.
- Web passwords are stored as salted PBKDF2-SHA256 hashes. Fresh installations and resets generate a random password; there is no fixed public default.
- Secure session cookies, origin checks, CSRF protection, local login throttling, and session invalidation protect browser workflows.
- The process runs without root privileges. The application validates command and variable allowlists, driver capabilities, identity, and dangerous-action confirmation before dispatch.
- A persistent request ledger guards against replaying a hardware mutation with the same request ID.
- The dedicated NUT account is limited to the application's supported instant-command list and NUT's `SET` permission. NUT does not make that `SET` grant specific to the three variables allowed by APCc.

These controls do not make an exposed management interface safe for the public internet. The application has one administrator role, no MFA or SSO, no independent external audit store, and no encrypted NUT transport. A host administrator can read protected local configuration and control the service. A NUT credential has the privileges granted by NUT even when used outside APCc.

## Handling sensitive data

Runtime configuration, inventory, audits, session material, certificates, private keys, and installation output can contain sensitive information. Keep them out of Git, public issues, screenshots, and release packages. The repository uses synthetic examples and ignores common runtime artifacts, but `.gitignore` is not a data-loss prevention system.

Back up configuration and state to encrypted storage. Review local log retention and access. Rotate the web password and the application-owned NUT password after an exposure; replacing a web password alone does not revoke a leaked NUT credential.

Before a security test, use a fake NUT server or an isolated test fixture. Never validate a vulnerability by power-cycling production equipment.
