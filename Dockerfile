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
      && add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
      && apt-get update -y \
      && apt-get install docker-ce docker-ce-cli -y\
      && curl -L "https://github.com/docker/compose/releases/download/1.29.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose\
      && ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose\
      && chmod +x /usr/local/bin/docker-compose\
      && pip install --user poetry

WORKDIR /app
COPY pyproject.toml poetry.lock /app/
ENV PATH="${PATH}:/root/.local/bin"
RUN poetry install
COPY . /
