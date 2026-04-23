FROM python:3.11-alpine

RUN apk add --no-cache curl bash dumb-init ca-certificates unzip

# rathole 0.5.0 ships the built-in file watcher as a default Cargo.toml
# feature. The agent triggers reloads by atomic-writing client.toml —
# no SIGHUP, no Docker socket. Connection preservation on unchanged
# services is verified in greffon/greffon docs/features/tunnel-support/spike/.
ARG RATHOLE_VERSION=0.5.0
RUN curl -fsSL -o /tmp/rathole.zip \
      "https://github.com/rapiz1/rathole/releases/download/v${RATHOLE_VERSION}/rathole-x86_64-unknown-linux-musl.zip" \
    && unzip /tmp/rathole.zip -d /usr/local/bin \
    && rm /tmp/rathole.zip \
    && chmod +x /usr/local/bin/rathole

WORKDIR /sidecar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/sidecar/entrypoint.sh"]
