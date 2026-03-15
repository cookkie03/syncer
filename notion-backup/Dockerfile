FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl \
    && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=0.2.29
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) ARCH="amd64" ;; \
      arm64) ARCH="arm64" ;; \
      *) echo "Unsupported arch: $TARGETARCH" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" \
      -o /usr/local/bin/supercronic && \
    chmod +x /usr/local/bin/supercronic

RUN pip install --no-cache-dir requests pyyaml google-api-python-client google-auth-httplib2 google-auth-oauthlib beautifulsoup4 "python-telegram-bot==20.*"

WORKDIR /app
COPY backup.py /app/backup.py
COPY download_export.py /app/download_export.py
COPY telegram_bot.py /app/telegram_bot.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /backup/json /backup/snapshots/daily /backup/snapshots/weekly /backup/zip_exports

ENTRYPOINT ["/app/entrypoint.sh"]
