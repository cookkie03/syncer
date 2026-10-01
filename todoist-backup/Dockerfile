FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=0.2.29
ARG TARGETARCH
# Synology/Debian x86 uses amd64; Apple Silicon/ARM hosts use arm64.
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) ARCH="amd64" ;; arm64) ARCH="arm64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" \
      -o /usr/local/bin/supercronic && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py storage.py collector.py backup.py layout.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
