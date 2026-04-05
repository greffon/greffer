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
RUN mkdir -p /app/data
CMD ["poetry", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]
