# Architecture

APCc is a single-host application with three distinct trust boundaries: browser to HTTPS proxy, proxy to local application, and application to NUT. The Linux installer provisions the first two and an application-owned account in an existing local NUT server.

## Request path

1. Apache accepts HTTPS on port `8443`, checks the operator address against the configured allowlist, and replaces the trusted forwarding headers.
2. The Python service accepts loopback HTTP on port `8765`. Authentication uses a server-side session and a `Secure`, `HttpOnly`, `SameSite=Strict` cookie. Mutation routes also validate the request origin and CSRF token.
3. Read requests use the NUT text protocol to obtain status, variables, and driver capabilities. Control requests additionally verify the configured UPS identity and the application's allowlists before authenticating to NUT.
4. For hardware mutations, SQLite records the request ID and dispatch intent before network transmission. Repeating the same ID does not dispatch the operation again. The result distinguishes NUT acceptance, rejection, and uncertain transport outcomes.
5. The browser displays available information and capability-driven controls. Static assets are served locally, with no external analytics or font requests.

The application uses Python's standard library: `http.server`, sockets, SQLite, cryptographic primitives, and filesystem operations. It does not execute shell commands to control the UPS. The systemd unit runs as an unprivileged account with write access limited to its state directory.

## Authentication and state

The installer generates the initial web password and stores a salted PBKDF2-SHA256 hash with 600,000 iterations. Password changes use an atomic private account-override file and invalidate active sessions. Sessions are in memory, expire after one hour, and disappear on restart. Failed logins are throttled locally.

Protected bootstrap configuration lives under `/etc/apcc/`; service-owned settings, the password override, audit records, and the request ledger live under `/var/lib/apcc/`. The NUT credential must be readable by the service and is not returned to the browser. Public configuration reports only whether a credential exists; an empty password field keeps the saved credential.

The audit stream records operator actions and outcomes. It is a local operational record, not a tamper-proof external audit service. A sufficiently privileged administrator, or a compromised application account, can modify files it owns.

## Capability and identity model

APCc intentionally exposes a bounded command set. A visible action must also be advertised by the connected NUT driver. Configuration writes are similarly restricted to supported, validated variables. A configured serial binds control to the intended UPS; the model label and inventory IP are descriptive metadata.

The outlet planner is independent of telemetry. User-entered watt estimates are never merged into measured UPS watts. Missing hardware measurements remain unavailable.

## Deployment limits

The application serves one configured UPS and one administrator account. It does not include role-based access control, MFA, SSO, fleet orchestration, distributed session storage, high availability, firmware flashing, automatic host shutdown coordination, or a cloud relay.

The NUT client uses ordinary TCP. The supplied local installation keeps that connection on loopback. If a remote NUT server is used, provide the required network protection outside APCc. Do not place the Python listener directly on an untrusted network or bypass the HTTPS proxy's header and address checks.

The request ledger reduces accidental duplicate dispatch; it cannot provide a distributed transaction with UPS hardware. Network failures can leave an outcome uncertain even when no retry is sent. Operators must verify physical and NUT state before deciding whether another action is needed.
