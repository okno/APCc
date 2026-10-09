# Contributing to APCc

Keep changes small, reviewable, and honest about hardware capabilities. APCc must never turn an unsupported action into a different physical command or present an estimate as a measurement.

## Local checks

Use Python 3.10 or later. The application and unit tests do not require Python packages from PyPI.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile app.py
bash -n install.sh
python3 scripts/check_public.py
```

Run tests against synthetic fixtures. Do not point a test suite or browser automation at a real UPS. For changes that affect dispatch, include coverage for unsupported commands, serial mismatch, request replay, transport uncertainty, and permission failures as appropriate.

## Pull requests

Explain the operational problem, the resulting behavior, and what you tested. Include synthetic screenshots for interface changes. Document compatibility limits and configuration migrations. Do not claim physical execution based only on a successful NUT response.

Keep the interface and documentation in English. Use locally served assets; do not add trackers, remote fonts, or a required cloud service. Respect the existing command boundary and avoid dependencies that do not materially improve the implementation.

Never submit installed configuration, actual hostnames, private IP addresses, UPS serials, user account details, credentials, or production screenshots. Documentation examples should use reserved names such as `example.invalid` and documentation addresses such as `192.0.2.1`. Keep runtime state outside the source tree.

Report security issues through the process in [SECURITY.md](SECURITY.md), not a public pull request containing a working exploit against a live system. Contributions are accepted under the project's [MIT license](LICENSE).
