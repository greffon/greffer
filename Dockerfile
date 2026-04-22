FROM python:3.11-alpine
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

WORKDIR /
RUN apk update \
      && apk add --no-cache \
            build-base \
            libffi-dev \
            docker \
            docker-compose \
      && pip install --no-cache-dir "poetry==1.4.2"

WORKDIR /app
COPY pyproject.toml poetry.lock /app/
ENV PATH="${PATH}:/root/.local/bin"
RUN poetry install --no-root
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
CMD ["sh", "-c", "poetry run python -m app.cli apply_ops_migrations && exec poetry run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --workers 1"]
