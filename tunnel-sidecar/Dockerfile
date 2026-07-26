FROM debian:bookworm-slim

# v3 sidecar is "rathole-client + dumb-init only" — no agent.py
# polling loop, no python runtime in the image. The greffer's
# FastAPI controller writes client.toml on the shared volume in
# response to manager pushes (POST /api/controller/tunnel-config/);
# rathole-client picks up the change via its built-in file watcher
# and connects out to rathole-server. See greffer#28 for the push
# endpoint and greffon/manager#33 for the manager side.
#
# slim doesn't include bash, curl, unzip, or dumb-init by default.
# procps provides pgrep, used by the healthcheck in docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash curl ca-certificates dumb-init procps unzip \
    && rm -rf /var/lib/apt/lists/*

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
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/sidecar/entrypoint.sh"]
