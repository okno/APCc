# Operating APCc

APCc is a control surface for an existing NUT installation. Before installation, confirm `upsc <ups-name>@127.0.0.1` returns a current `ups.status` and a serial number. The installer requires a serial so it can bind the initial configuration to the intended UPS. Choose the correct NUT driver using the documentation for your actual hardware.

## Installed components

| Path or service | Purpose |
| --- | --- |
| `/opt/apcc/` | Root-owned application and static assets. |
| `/etc/apcc/config.json` | Bootstrap settings, salted administrator password hash, and NUT service credential; mode `0640`, root-owned and readable by the service group. |
| `/etc/apcc/tls.crt`, `/etc/apcc/tls.key` | Locally generated certificate and private key; the key is mode `0600`. |
| `/var/lib/apcc/` | Service-owned private settings, account override, audit, and SQLite request ledger; directory mode `0700`. |
| `apcc.service` | Unprivileged systemd service with filesystem and process restrictions. |
| `/etc/apache2/sites-available/apcc.conf` | HTTPS virtual host and explicit address allowlist. |
| `/etc/apache2/conf-available/apcc-listen.conf` | Dedicated HTTPS listener on port `8443`. |
| `/etc/nut/upsd.users` | One bounded, installer-managed `apcc` account block. Other account definitions are preserved. |

Do not commit any installed files or host-derived output. The source tree contains no installation credentials or real device inventory.

The installer refuses unrecognized files at its Apache paths and unrelated listeners on its required ports. It preserves previous application directories and makes root-readable NUT/Apache backups when modifying them. It does not disable existing NUT CGI applications, alter firewall rules, configure `upsmon`, or restart the driver. Review any other control interfaces on your host separately.

## Access and certificates

By default only `127.0.0.1` and `::1` pass the Apache access rule. This is an authorization rule: Apache may listen on other host interfaces, but requests from other addresses are denied. Use a dedicated management network or VPN and an explicit `APCC_ALLOWED_CIDR` to allow remote operators. A `/0` rule is rejected.

```bash
# Reserved documentation address; substitute your management subnet.
sudo env APCC_ALLOWED_CIDR='192.0.2.0/24' bash install.sh
```

The installer generates a self-signed certificate on the host and reports its SHA-256 fingerprint. Compare that fingerprint through a trusted channel before accepting it. For routine managed deployments, replace the certificate and key with a pair issued by your internal CA. Preserve root ownership and private-key permissions, validate with `sudo apache2ctl configtest`, and reload Apache. The installer preserves an existing valid certificate and key pair.

Keep the application backend bound to loopback. Apache overwrites `X-Forwarded-Proto` and `X-Panel-Client-IP`; accepting those headers directly from arbitrary clients would break the intended trust boundary. This deployment expects Apache to receive the operator connection directly. An additional proxy needs an explicit review of client-address attribution and access rules.

## Account recovery and credential rotation

Fresh installation creates the username `admin` with a random 32-character password. It is displayed once and persists until changed. Only its salted PBKDF2-SHA256 hash is saved as the web authentication credential.

Change the web password from Configuration. A successful change invalidates all current sessions. If access is lost, run the following from the matching source or release directory on the host:

```bash
sudo bash install.sh --reset-admin
```

This generates a new random password, resets the account to `admin`, clears the service's password override, and restarts the service to revoke sessions. It preserves UPS and outlet settings. Recognized installer credential backups are removed; administrator-created backups remain your responsibility.

Rotate the application-owned local NUT credential with:

```bash
sudo bash install.sh --rotate-nut-password
```

Both flags can be combined. Rotation replaces the password only for the managed local `apcc` NUT account and updates a saved local connection using that account. It does not rotate credentials for a separately configured remote NUT server. The NUT password is separate from the web password and must remain available in the protected configuration so the application can authenticate to NUT.

Do not pipe installation output into a public log: a new administrator password is shown at the end. If an installation fails before that output, rerun with `--reset-admin` after resolving the failure.

## Configuration and identity

The web configuration contains two different kinds of data:

- **Display and inventory data:** friendly name, model label, inventory IP, refresh interval, and outlet annotations. These do not change the UPS hardware.
- **Connection and identity data:** NUT host, port, UPS name, service credential, and expected serial. Changes are probed before being saved. The probe reads status and checks authentication without issuing a hardware command.

Use **Test connection** before changing an endpoint. APCc checks the device serial before control actions and fails when it does not match the configured identity. The installer's initial discovery is local; a remote NUT endpoint can be configured afterward within the application's permitted private-address ranges. The current transport is ordinary NUT TCP, not TLS. Use a trusted local network or an encrypted tunnel when moving beyond loopback.

Changing the inventory IP does not set the IP of an Ethernet management card or SmartConnect port. Changing the model label does not change the model reported by the driver.

## Capability boundaries

APCc combines its command allowlist with `LIST CMD` and `LIST RW` reported by NUT. Missing capabilities remain unavailable. The application currently supports these actions when the driver exposes them:

| Command | Effect |
| --- | --- |
| `beeper.enable`, `beeper.disable`, `beeper.mute` | Enable, disable, or temporarily silence audible alarms. |
| `beeper.on`, `beeper.off` | Legacy alarm aliases; prefer the modern commands. |
| `test.panel.start` | Request the UPS panel test, which may include sound and indicator lights. |
| `load.off` | Cut output power immediately. |
| `load.off.delay` | Request output power-off using the configured shutdown delay, sent explicitly with the command. |
| `shutdown.reboot` | Request an output power cycle. |
| `shutdown.stop` | Request cancellation of a pending shutdown when the UPS still allows it. |

The three supported writable values are `battery.charge.low`, `battery.runtime.low`, and `ups.delay.shutdown`. A driver may advertise fewer of them. Range checks apply in APCc, and the driver can reject values it does not support.

A positive NUT response means the request was accepted. It does not verify physical output state or guarantee cancellation. APCc records dispatch intent before sending a mutation and prevents the same request ID from being sent again. If an operation's result is uncertain, inspect the actual UPS state before creating a new request.

Power-off and power-cycle actions interrupt power to attached loads. They are not Linux reboot commands and do not gracefully stop operating systems. Configure NUT monitoring and coordinated shutdown separately, and use an appropriate maintenance window for live power tests.

### Beeper testing

The beeper test stays unavailable unless `test.panel.start` appears in the driver's supported commands. Enabling an alarm is not an acoustic test, and APCc does not simulate a tone by changing power state. A supported panel test may also flash front-panel indicators. Consult the hardware manual before using it.

### Outlet and power data

Measured UPS watts come from `ups.realpower` or `output.realpower` when provided; apparent power and load percentage are separate quantities. A missing measurement is shown as unavailable, never as zero.

Outlet device names and watt values are editable planning records. The total of entered watt estimates remains labeled as an estimate. The current application does not provide per-outlet metering, energy accumulation in kWh, or individual outlet switching. Do not treat a logical inventory row as a detected physical connector or a measured circuit.

### Firmware and cloud features

APCc does not update UPS firmware, provision SmartConnect, or replace the features of a manufacturer network management card. Those workflows depend on the model and supported manufacturer tools. It does not promise every function a UPS could expose.

## Backup, upgrade, and recovery

Back up `/etc/apcc/` and `/var/lib/apcc/` to encrypted storage. They contain device identities, credentials, private keys, and operational history. For a consistent copy of the SQLite ledger, stop only `apcc.service` while taking the backup, then start it again. NUT monitoring can remain running.

When upgrading, review the release notes, extract the new package, and run `sudo bash install.sh` from it. The normal path preserves web credentials, mutable settings, and the existing Apache allowlist. A service restart invalidates in-memory sessions. The installer is not a transactional system-wide upgrade; inspect its error output if it stops partway through.

To restore a backup, stop the application, restore its configuration and state with the original ownership and permissions, and validate Apache before starting the service. Coordinate NUT credential restoration with the managed `apcc` account; restoring only one side will make authentication fail. Verify UPS identity and read-only status before resuming control operations. Do not reuse the same stale request ledger across unrelated UPS installations.

Useful local diagnostics:

```bash
systemctl status apcc --no-pager
sudo journalctl -u apcc -n 50 --no-pager
sudo apache2ctl configtest
upsc apc@127.0.0.1
```

These commands can reveal hostnames, addresses, serials, and operating details. Redact them before sharing any output. Audit logs and backups require your own retention policy; the application does not currently provide automatic audit rotation or high availability.
