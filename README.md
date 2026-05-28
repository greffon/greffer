# Greffon Greffer

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2.svg)](https://discord.gg/vBmhUGPY)

The worker node for [Greffon](https://greffon.io), a self-hosted app deployment platform. A greffer runs on a machine you own (VPS, mini-PC, Raspberry Pi, old laptop — anything with Docker), receives commands from the [manager](https://github.com/greffon/manager) control plane, and deploys/manages greffon instances via docker-compose behind a TLS reverse proxy.

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

The greffer runs as a single uvicorn worker (`--workers 1`) on purpose: three background tasks (register / monitor / CRL sync) live in one process, and multi-worker would spawn duplicate copies that fight over cert state.

## Local development

The greffer needs a reachable [manager](https://github.com/greffon/manager) and a Docker daemon.

```bash
poetry install
poetry run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8001
```

Greffer runs on `localhost:8001`. Boot runs ops-migrations (`python -m app.cli apply_ops_migrations`) before uvicorn binds, so on-disk state migrations can't race request handlers.

Configuration is via environment variables (`GREFFON_BASE_SERVER`, `GREFFER_ID`, `GREFFER_PROTOCOL`, etc.) — see `env.env` for the full set.

## API

FastAPI auto-generates OpenAPI docs. With the greffer running locally, see `http://localhost:8001/docs` (Swagger UI) and `/redoc`.

## License

[AGPL v3](LICENSE). Network copyleft protects against commercial clones running a greffer fleet as a hosted service. For internal/self-hosted use there's no disclosure trigger — you can run, modify, and deploy your own greffer freely. Greffon's policy in six words: *free to self-host, pay to resell.*

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) — DCO sign-off (`git commit -s`), conventional commits, `ruff format`, pytest.

## Community

[Discord](https://discord.gg/vBmhUGPY) · file bugs and feature requests in this repo's [Issues](https://github.com/greffon/greffer/issues) · [Code of Conduct](CODE_OF_CONDUCT.md)

## Security

Report privately via [Security Advisories](https://github.com/greffon/greffer/security/advisories/new) or `security@greffon.io`. See [SECURITY.md](SECURITY.md). The greffer handles mTLS certs, runs Docker, and proxies user traffic — security reports here are high-priority.
