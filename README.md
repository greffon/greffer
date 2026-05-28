# greffer

Worker service that receives deployment commands from manager and runs greffon workloads with Docker Compose.

## Responsibilities

- register to manager backend
- receive `start`/`stop` commands
- fetch remote compose templates
- render per-instance compose/nginx config
- apply user configuration (`env`/`json`/`file`)
- report status changes back to manager

## Environment

Configure `env.env`:

- `GREFFON_BASE_SERVER`
- `GREFFON_PATH`
- `GREFFER_ADDRESS`
- `GREFFER_PORT`
- `GREFFER_PROTOCOL`
- `GREFFER_ID`
- `GREFFER_SSL_VERIFY`
- optional `DOCKER_NGINX_NAME`

## Run With Docker

```bash
cd greffer
docker compose up --build
```

Services started:
- `greffer` (Django API)
- `nginx` (TLS proxy, default host port `8001`)

## Local Python Run

```bash
cd greffer
poetry install
poetry run python manage.py migrate
poetry run python manage.py runserver
```

Note: full start/stop orchestration requires Docker socket access and `docker-compose` binary.

## API

Base path: `/api/controller/`
- `POST /start/`
- `POST /stop/`
- `GET /greffon/<uuid:id>/`

## Related Docs

- `../docs/greffer.md`
- `../manager/docs/architecture.md`
