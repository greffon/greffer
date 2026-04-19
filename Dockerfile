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
RUN poetry install --no-root && poetry run pip install "setuptools<78"
COPY . /app
# Run pending ops migrations BEFORE the HTTP surface opens so they can't race
# with request handlers that touch $GREFFON_PATH. Non-blocking on soft failure:
# the framework's partial-apply prevention means next boot retries.
CMD ["sh", "-c", "poetry run python manage.py apply_ops_migrations; exec poetry run python manage.py runserver 0.0.0.0:8000"]
