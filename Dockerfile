FROM python:3
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

WORKDIR /
RUN apt-get update -y  \
      && apt-get install apt-transport-https                                           \
                    ca-certificates                                               \
                    curl                                                          \
                    gnupg-agent                                                   \
                    software-properties-common -y                                 \
      && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -  \
      && add-apt-repository "deb [arch=$(dpkg --print-architecture)] https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
      && apt-get update -y \
      && apt-get install docker-ce docker-ce-cli -y\
      && pip install --user poetry docker-compose

WORKDIR /app
COPY pyproject.toml poetry.lock /app/
ENV PATH="${PATH}:/root/.local/bin"
RUN poetry install
COPY . /
