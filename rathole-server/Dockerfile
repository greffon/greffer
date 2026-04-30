FROM debian:bookworm-slim

# Tools needed at build time + runtime: unzip for extracting rathole,
# ca-certificates for the curl HTTPS download, bash for entrypoint.sh,
# dumb-init as PID 1 so SIGTERM/SIGINT propagate cleanly through to
# rathole and zombie children get reaped.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash curl ca-certificates dumb-init unzip \
    && rm -rf /var/lib/apt/lists/*

# rathole 0.5.0's published Linux artefacts are asymmetric: amd64 ships
# only as glibc-linked (x86_64-unknown-linux-gnu), arm64 only as a
# statically-linked musl build. Both run on this Debian-slim base —
# the gnu binary finds glibc, and the musl binary is fully static so
# the host libc is irrelevant. Same dispatch we use in
# greffer/tunnel-sidecar/Dockerfile.
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

WORKDIR /srv
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/srv/entrypoint.sh"]
