FROM python:3.12-slim

ARG MYGITHUB10_BUILD_SHA=unknown

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYGITHUB10_BUILD_SHA=${MYGITHUB10_BUILD_SHA}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data /app/config

COPY requirements.txt .
RUN pip install --no-cache-dir --no-input -r requirements.txt

COPY app/ ./app/
COPY tests/ ./tests/
COPY config/ ./config/
COPY scripts/ ./scripts/
RUN mkdir -p /app/docs \
    && python scripts/generate_tool_manifest.py --source /app --source-commit unknown --service-name MyGithub10 --output /app/docs/MYGITHUB10_TOOL_MANIFEST.json

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
