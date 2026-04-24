FROM python:3.11-alpine

RUN apk add --no-cache curl bash dumb-init ca-certificates unzip

# rathole 0.5.0 ships the built-in file watcher as a default Cargo.toml
# feature. The agent triggers reloads by atomic-writing client.toml —
# no SIGHUP, no Docker socket. Connection preservation on unchanged
# services is verified in greffon/greffon docs/features/tunnel-support/spike/.
#
# TARGETARCH is set automatically by Docker BuildKit / buildx for the
# target platform, so multi-arch builds (amd64 + arm64) download the
# correct rathole asset. Unknown architectures fail the build loudly
# rather than embedding a binary that segfaults at runtime.
ARG TARGETARCH
ARG RATHOLE_VERSION=0.5.0
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) RATHOLE_ARCH=x86_64-unknown-linux-musl ;; \
      arm64) RATHOLE_ARCH=aarch64-unknown-linux-musl ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH:-<unset>}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/rathole.zip \
      "https://github.com/rapiz1/rathole/releases/download/v${RATHOLE_VERSION}/rathole-${RATHOLE_ARCH}.zip"; \
    unzip /tmp/rathole.zip -d /usr/local/bin; \
    rm /tmp/rathole.zip; \
    chmod +x /usr/local/bin/rathole

WORKDIR /sidecar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/sidecar/entrypoint.sh"]
