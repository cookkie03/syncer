FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gettext-base \
    && rm -rf /var/lib/apt/lists/*

# Install supercronic for cron scheduling
ARG SUPERCRONIC_VERSION=0.2.29
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) ARCH="amd64" ;; \
      arm64) ARCH="arm64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" \
      -o /usr/local/bin/supercronic && \
    chmod +x /usr/local/bin/supercronic

# Install vdirsyncer with Google extras.
# Pin aiohttp<3.9: 3.9 changed session/connector lifecycle causing
# "Session is closed" errors during concurrent calendar syncs with
# vdirsyncer 0.20.x. Python 3.11 has prebuilt cp311 wheels for 3.8.x
# so no C compiler is needed in the image.
RUN pip install --no-cache-dir "vdirsyncer[google]" "aiohttp>=3.8.1,<3.9"

WORKDIR /app
COPY config.template /app/config.template
COPY entrypoint.sh /app/entrypoint.sh
COPY sync-notify.sh /app/sync-notify.sh
RUN chmod +x /app/entrypoint.sh /app/sync-notify.sh

# Vdirsyncer config and status directories
RUN mkdir -p /data/status /data/token

ENTRYPOINT ["/app/entrypoint.sh"]
