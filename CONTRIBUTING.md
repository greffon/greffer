# Contributing to Greffon Greffer

Thanks for considering a contribution. This repo is the worker node for [Greffon](https://greffon.io) — it runs on user hardware, talks to the [manager](https://github.com/greffon/manager), and drives Docker.

## Quick checklist

- Read the [Code of Conduct](./CODE_OF_CONDUCT.md)
- For substantial changes, open an issue first to discuss
- Sign your commits with DCO: `git commit -s` (see below)
- Use [conventional commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`
- One PR = one logical change
- Open a PR against `main`

## Developer Certificate of Origin (DCO)

By signing off your commits you certify that you wrote the code (or have the right to submit it) and license it to the project under [AGPL v3](./LICENSE). Full text at [developercertificate.org](https://developercertificate.org/).

```bash
git commit -s -m "feat: add CRL refresh backoff"
```

PRs without DCO sign-off cannot be merged.

- Forgot the last commit? `git commit --amend -s --no-edit && git push --force-with-lease`
- Multiple commits? `git rebase --signoff main && git push --force-with-lease`

## Local setup

```bash
poetry install
poetry run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8001
```

The greffer expects a reachable [manager](https://github.com/greffon/manager) and a Docker daemon. See [README.md](./README.md) for the env vars and run command.

## Code style

- Python 3.12+, FastAPI, Pydantic v2, asyncio
- `ruff format` enforced (runs on save in supported editors)
- No bare `except:` — catch specific exceptions and log
- Use `transaction`-style atomic patterns for multi-step on-disk state changes
- Never `verify=False` on `requests`/`httpx` calls
- Secrets from environment variables only — never hardcoded
- Respect the `--workers 1` invariant: background tasks (register / monitor / CRL sync) assume a single process

## Tests

```bash
poetry run pytest
```

Add tests for new behavior. Bug fixes should include a regression test where practical. CLI changes have integration smoke tests (mocked Docker + mock manager).

## Pull request review

- One maintainer review required before merge
- CI must pass (lint + tests)
- Changes that touch the manager contract: link the corresponding [manager](https://github.com/greffon/manager) PR

## Reporting bugs

[Open a GitHub issue](https://github.com/greffon/greffer/issues/new/choose) using the bug report template.

## Security vulnerabilities

**Do not file a public issue.** The greffer handles mTLS certs, runs Docker, and proxies user traffic — see [SECURITY.md](./SECURITY.md) for the private channel.

## Questions

- Bugs and feature requests: this repo's [Issues](https://github.com/greffon/greffer/issues)
- Real-time / general: [Discord](https://discord.gg/vBmhUGPY)
