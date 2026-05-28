# Greffon Greffer

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2.svg)](https://discord.gg/vBmhUGPY)

The worker node for the [Greffon](https://github.com/greffon/greffon) self-hosted app deployment platform. A greffer runs on a machine you own (VPS, mini-PC, Raspberry Pi, old laptop — anything with Docker), receives commands from the [manager](https://github.com/greffon/manager) control plane, and deploys/manages greffon instances via docker-compose behind a TLS reverse proxy.

**Tech stack:** FastAPI 0.110, uvicorn (`--workers 1` by design), Pydantic v2, asyncio, the Docker SDK, and Nginx for per-instance TLS.

## What it does

- Registers itself with the manager and installs the mTLS cert the manager issues
- Downloads catalog compose templates, renders them (Jinja2), and runs `docker compose up`
- Reverse-proxies each greffon instance over TLS (per-instance Nginx config)
- Monitors instance status and posts callbacks to the manager
- Optionally runs in **tunnel mode** (the Stem) via a rathole sidecar — roadmap, not GA

## Architecture

```
Manager (control plane)
   │  mTLS, HTTPS
   ▼
THIS REPO — greffer (FastAPI, asyncio)
   │
   ▼
Docker Engine (docker-compose) ──► greffon instances
   │
   ▼
Nginx (per-instance TLS reverse proxy)
```

The greffer runs as a single uvicorn worker (`--workers 1`) on purpose: three background tasks (register / monitor / CRL sync) live in one process, and multi-worker would spawn duplicate copies that fight over cert state. See [docs](https://github.com/greffon/greffon/blob/main/docs/greffer.md).

## Local development

Fastest path is the monorepo's `setup-dev.sh`:

```bash
git clone --recurse-submodules https://github.com/greffon/greffon
cd greffon && ./scripts/setup-dev.sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

Greffer runs on `localhost:8001`. For standalone dev:

```bash
poetry install
poetry run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8001
```

Boot runs ops-migrations (`python -m app.cli apply_ops_migrations`) before uvicorn binds — see [the boot flow](https://github.com/greffon/greffon/blob/main/docs/greffer.md).

## API

FastAPI auto-generates OpenAPI docs. With the greffer running locally, see `http://localhost:8001/docs` (Swagger UI) and `/redoc`.

## License

[AGPL v3](LICENSE) — same as the rest of the Greffon product. Network copyleft protects against commercial clones running a greffer fleet as a hosted service. For internal/self-hosted use there's no disclosure trigger. [Licensing rationale](https://github.com/greffon/greffon/blob/main/docs/marketing/licensing.md).

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) — DCO sign-off (`git commit -s`), conventional commits, `ruff format`, pytest.

## Community

[Discord](https://discord.gg/vBmhUGPY) · [GitHub Discussions](https://github.com/greffon/greffon/discussions) · [Code of Conduct](CODE_OF_CONDUCT.md)

## Security

Report privately via [Security Advisories](https://github.com/greffon/greffer/security/advisories/new) or `security@greffon.io`. See [SECURITY.md](SECURITY.md). The greffer handles mTLS certs, runs Docker, and proxies user traffic — security reports here are high-priority.
