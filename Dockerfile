# Stage 1: build the dependency venv. build-base/libffi-dev are only
# needed to compile wheels at install time; keeping them out of the
# runtime image saves ~250MB.
FROM python:3.11-alpine AS builder
RUN apk add --no-cache build-base libffi-dev \
      && pip install --no-cache-dir "poetry==1.8.5"
# Poetry 1.8+ is required because pyproject.toml uses `package-mode = false`
# (introduced in 1.8.0). With 1.4.2 the container build fails at
# `poetry install` with an unknown-key parse error.

WORKDIR /app
COPY pyproject.toml poetry.lock /app/
# In-project venv so the runtime stage can copy /app/.venv wholesale and
# run it without poetry installed at all.
ENV POETRY_VIRTUALENVS_IN_PROJECT=1
# --only main: the dev group (pytest, factory-boy/faker, httpx, the
# setuptools<70 Python-3.12 distutils workaround) is ~25MB of packages
# the 3.11 runtime never imports. pip/wheel are virtualenv seeds, not
# deps — nothing installs packages at runtime, so drop them too.
RUN poetry install --no-root --only main \
      && rm -rf /app/.venv/lib/python3.11/site-packages/pip \
                /app/.venv/lib/python3.11/site-packages/pip-* \
                /app/.venv/lib/python3.11/site-packages/wheel \
                /app/.venv/lib/python3.11/site-packages/wheel-* \
                /app/.venv/bin/pip* /app/.venv/bin/wheel*

# Stage 2: runtime. docker-cli + the compose plugin only — greffer talks
# to the HOST daemon through the mounted /var/run/docker.sock, so the
# full `docker` package (dockerd, containerd, runc) is never used. The
# compose v2 binary also works standalone, so symlinking it into PATH
# keeps the `docker-compose` executable name the app shells out to
# (apps/utils/docker/compose.py).
FROM python:3.11-alpine
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apk add --no-cache docker-cli docker-cli-compose \
      && ln -s /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

# Compat shim: poetry itself is no longer installed (the venv is already
# on PATH), but compose command overrides in the wild — including the
# e2e harness's generated override — still say `poetry run <cmd>`. Map
# that to a plain exec so they keep working; reject anything else loudly.
RUN printf '%s\n' \
      '#!/bin/sh' \
      'if [ "$1" = "run" ]; then shift; exec "$@"; fi' \
      'echo "poetry is not installed in this slim image; only \"poetry run <cmd>\" is shimmed" >&2' \
      'exit 1' \
      > /usr/local/bin/poetry \
      && chmod +x /usr/local/bin/poetry

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
COPY . /app

# Run pending ops migrations BEFORE uvicorn binds, so they can't race with
# request handlers that touch $GREFFON_PATH. `&&` (not `;`): if
# apply_ops_migrations exits non-zero, refuse to start the server — safer
# than the pre-cutover `;` which started anyway.
#
# --workers 1 is a hard requirement: multi-worker uvicorn would spawn N
# copies of each background task (register / monitor / CRL sync), each
# minting its own token and fighting the manager over cert state. See
# HLD #3 § Single-worker uvicorn constraint.
CMD ["sh", "-c", "python -m app.cli apply_ops_migrations && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --workers 1"]
