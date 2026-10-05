FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    FOIA_DB_PATH=/data/foia_archive.db \
    FOIA_FILES_DIR=/data/files

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gosu \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

RUN groupadd --gid 10001 foia \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh foia \
    && mkdir -p /data/files \
    && chown -R foia:foia /data

COPY . .
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec python -m uvicorn ui.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
