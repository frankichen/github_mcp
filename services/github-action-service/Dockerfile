FROM python:3.12-slim

ARG SOURCE_REPOSITORY=https://github.com/frankichen/github_mcp
ARG VCS_REF
ARG SERVICE_VERSION
ARG BUILD_DATE

RUN python -c "import re,sys; sys.exit(0 if re.fullmatch(r'[0-9a-f]{40}', sys.argv[1]) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', sys.argv[2]) else 1)" "$VCS_REF" "$SERVICE_VERSION"

LABEL org.opencontainers.image.source="$SOURCE_REPOSITORY" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.version="$SERVICE_VERSION" \
      org.opencontainers.image.created="$BUILD_DATE"

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYGITHUB10_BUILD_SHA="$VCS_REF" \
    MYGITHUB10_VERSION="$SERVICE_VERSION"

RUN apt-get \
        -o Acquire::Retries=3 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        update \
    && apt-get \
        -o Acquire::Retries=3 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
        install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data /app/config

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --no-input -c constraints.txt -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/
COPY scripts/ ./scripts/

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
