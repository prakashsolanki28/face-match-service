# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="FaceMatch" \
      org.opencontainers.image.description="Lightweight self-hosted face detection and face matching service" \
      org.opencontainers.image.source="https://github.com/prakashsolanki28/face-match-service" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    MODELS_DIR=/opt/face-match/models \
    AUTO_DOWNLOAD_MODELS=false

# Pick up OS security fixes released after the base image was built.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/face-match
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY face_match ./face_match
RUN pip install ".[postgres]" \
 # Bake the models (SHA-256 verified) into the image so containers start offline.
 && face-match download-models \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin facematch \
 && mkdir -p /data && chown facematch /data

USER facematch
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
# Listens on 0.0.0.0, so API_KEY is required (or ALLOW_NO_AUTH=true to run open on purpose).
CMD ["face-match", "serve"]
