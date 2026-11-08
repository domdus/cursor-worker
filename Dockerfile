FROM python:3.12-slim-bookworm

ARG VERSION=dev

LABEL \
  org.opencontainers.image.source="https://github.com/domdus/cursor-worker" \
  org.opencontainers.image.description="Headless Cursor SDK agent: MCP tool, job API and OpenAI-compatible chat" \
  org.opencontainers.image.licenses="MIT" \
  org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8787

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client rsync curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 worker \
    && mkdir -p /data && chown worker:worker /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cursor_worker ./cursor_worker

USER worker
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" -H "Host: 127.0.0.1" >/dev/null || exit 1
CMD ["python", "-m", "cursor_worker.app"]
