# greffer-cli

Operator CLI for installing and managing a greffer.

A greffer is a server you own that runs self-hosted apps from Greffon's
catalog. The CLI replaces today's manual `git clone + edit env.env +
docker compose up + ask admin to accept` flow with a single `greffer up`
command the manager admin hands operators.

## Status

**Pre-release.** Not yet packaged as a binary. Today you run the CLI
from this directory via Poetry:

```bash
cd cli
poetry install
poetry run greffer --help
```

The PyInstaller binary distribution, the `install.sh` / `install.ps1`
shims served at `greffon.io/install.sh`, and the cross-OS E2E suite all
land in a follow-up PR. This PR ships the Python package and its unit
tests; integration with the release infrastructure follows.

## Commands

| Command | Purpose |
| --- | --- |
| `greffer doctor` | Read-only preflight check |
| `greffer install-deps` | Detect Docker; print install instructions if missing |
| `greffer up --id <UUID>` | All-in-one: write config + start container + register with manager |
| `greffer status` | Report current state |

See the epic + HLD in [`docs/features/greffer-cli/`](../../docs/features/greffer-cli/)
for the full design — operator-facing string examples, state machine,
mode semantics (tunnel vs proxy), and the manager-side endpoints the
CLI consumes.

## Supported platforms (post-PyInstaller release)

| OS / arch | v1 binary | Notes |
| --- | --- | --- |
| linux-x86_64 (glibc) | ✓ | VPS / homelab |
| linux-arm64 (glibc) | ✓ | Pi 4+, Apple Silicon servers, Graviton |
| darwin-x86_64 | ✓ | Intel Macs |
| darwin-arm64 | ✓ | Apple Silicon Macs |
| windows-x86_64 | ✓ | Windows 10/11 with Docker Desktop |
| Pi 3 / armv7 / armv6 | — | Build from source: `cd cli && poetry install && poetry run pyinstaller …` |
| Alpine / musl | — | Same — glibc binaries do not run on musl |
| FreeBSD / OpenBSD | — | Out of scope (Docker support is unofficial) |
