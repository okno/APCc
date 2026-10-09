# APCc 3.0.0

The first public APCc release brings an English local UPS administration
interface to Linux hosts with an existing Network UPS Tools installation.

- A status dashboard with explicit unavailable readings and runtime caveats.
- Capability-checked alarm and output controls, typed power-off confirmation,
  serial identity matching, and a persistent request ledger.
- Editable panel metadata, NUT connection settings, password management, and
  an outlet inventory that separates estimates from measured power.
- A Linux installer with a dedicated unprivileged service, HTTPS proxy,
  explicit network allowlists, and a randomly generated initial admin password.
- Original vector branding, presentation artwork, and synthetic screenshots.
- Operations, architecture, security, and contribution documentation.

Validation includes 45 isolated Python tests, installer syntax and configuration
migration checks, a fully mocked desktop/mobile browser check, and a public-data
scan. No validation command needs to contact actual UPS hardware.

The Linux archive is accompanied by SHA256SUMS. Extract the archive, then read
README.md before running `sudo bash install.sh`. Existing NUT configuration
is required. Hardware capabilities remain dependent on the UPS and driver;
firmware updates, per-outlet metering, and graceful OS shutdown coordination
are outside APCc.
