FROM python:3.11-slim

# slim doesn't include bash, curl, unzip, or dumb-init by default.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash curl ca-certificates dumb-init unzip \
    && rm -rf /var/lib/apt/lists/*

# rathole 0.5.0 ships the built-in file watcher as a default Cargo.toml
# feature. The agent triggers reloads by atomic-writing client.toml —
# no SIGHUP, no Docker socket. Connection preservation on unchanged
# services is verified in greffon/greffon docs/features/tunnel-support/spike/.
#
# rathole 0.5.0's published Linux artefacts are asymmetric: amd64 ships
# only as glibc-linked (x86_64-unknown-linux-gnu), arm64 only as a
# statically-linked musl build. The Debian-slim base satisfies both —
# the gnu binary finds glibc, and the musl binary is fully static so
# the host libc is irrelevant. Alpine would require building rathole
# from source for amd64; not worth the build-time cost.
#
# TARGETARCH is populated by BuildKit / buildx for the target platform,
# so multi-arch builds pull the correct asset. Unsupported arches fail
# the build loudly instead of embedding a binary that segfaults at runtime.
ARG TARGETARCH
ARG RATHOLE_VERSION=0.5.0
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) RATHOLE_ARCH=x86_64-unknown-linux-gnu ;; \
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
