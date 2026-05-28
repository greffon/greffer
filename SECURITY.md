# Security Policy

The Greffon team takes security seriously and appreciates responsible disclosure. The greffer is a high-trust component — it holds mTLS certificates, controls a Docker daemon, and reverse-proxies user traffic — so security reports here are high-priority.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions, or pull requests.**

Use one of these private channels:

- **GitHub Security Advisories** (preferred): [open a private advisory](https://github.com/greffon/greffer/security/advisories/new)
- **Email**: `security@greffon.io` — PGP key available on request

Include as much as you can:

- Type of vulnerability (e.g., RCE via compose rendering, cert/key exposure, SSRF, container escape, privilege escalation)
- Affected files and step-by-step reproduction
- Proof-of-concept, if available
- Impact assessment

## What to expect

- **Acknowledgement**: within 72 hours
- **Initial assessment**: within 7 days
- **Fix timeline**: severity-dependent; critical issues (cert/key exposure, RCE, container escape) are fast-tracked
- **Disclosure**: coordinated with you, typically after a fix ships

## Scope — especially relevant for the greffer

- mTLS certificate / private key handling and the CRL sync path
- Jinja2 rendering of catalog compose templates (template injection, arbitrary host paths)
- Docker SDK usage (container escape, host mount exposure)
- The Nginx per-instance proxy config generation
- Tunnel mode / rathole sidecar (when enabled)
- Auth on manager callbacks and the cert-poll endpoint

Related repos each have their own SECURITY.md: [manager](https://github.com/greffon/manager), [manager-front](https://github.com/greffon/manager-front), [greffon-catalog](https://github.com/greffon/greffon-catalog). Anything sent to `security@greffon.io` will be routed.

## Out of scope

- Vulnerabilities in third-party dependencies (report upstream; tell us if a CVE affects us)
- Vulnerabilities in the deployed greffon applications themselves (report to the upstream app)
- Social engineering, physical attacks, attacks requiring host root the operator already granted

## Safe harbor

We support good-faith security research. If you act in accordance with this policy, we won't pursue legal action: don't access more data than necessary to demonstrate the issue, don't exfiltrate user data, and don't run automated scanners against production greffers without coordinating first.
