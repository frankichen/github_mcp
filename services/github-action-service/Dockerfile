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
    && python -c 'import asyncio,json; from pathlib import Path; from app.mcp_server import mcp; tools=asyncio.run(mcp.list_tools()); items=[]; [items.append({"name": t.name, "read_only": t.name.startswith(("get_","list_","compare_","search_","diagnose_","plan_")) and not t.name.startswith(("create_","update_","delete_","merge_","mark_","convert_","start_","cancel_","rollback_")), "consequential": not (t.name.startswith(("get_","list_","compare_","search_","diagnose_","plan_")) and not t.name.startswith(("create_","update_","delete_","merge_","mark_","convert_","start_","cancel_","rollback_"))), "category": "github", "description": t.description or "", "input_schema": t.inputSchema}) for t in tools]; Path("/app/docs/MYGITHUB10_TOOL_MANIFEST.json").write_text(json.dumps({"service_name":"MyGithub10","source_tree_sha":"unknown","generated_from_commit":"unknown","generated_at":"unknown","controller_image":"not-read-from-runtime","pygithub_version":"2.5.0","mcp_sdk_version":"mcp>=1.7.0 (requirements baseline)","tool_count":len(items),"tools":items},ensure_ascii=False,indent=2)+chr(10),encoding="utf-8")'

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
