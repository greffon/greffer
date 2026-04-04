FROM python:3.11-alpine
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

WORKDIR /
RUN apk update  \
      && apk add docker docker-compose poetry

WORKDIR /app
COPY pyproject.toml poetry.lock /app/
ENV PATH="${PATH}:/root/.local/bin"
RUN poetry install --no-root && poetry run pip install "setuptools<78"
COPY . /app
CMD ["poetry", "run", "python", "manage.py", "runserver", "0.0.0.0:8000"]
