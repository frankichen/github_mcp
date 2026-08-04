import logging
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi

from app.routers import health, github, ci_worker, deployments, ci_monitor, index_v12
from app.config import settings
from app.exceptions import AppError
from app.idempotency import IdempotencyMiddleware, ensure_idempotency_storage
from app.oauth import get_oauth_protected_resource_metadata
from app.observability import RequestObservabilityMiddleware
from app.version import SERVICE_NAME, SERVICE_VERSION, validate_runtime_metadata

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_metadata()
    os.makedirs(os.path.dirname(settings.IDEMPOTENCY_DB_PATH), exist_ok=True)
    try:
        from app.ci_database import init_db
        from app.deployment_service import init_deployment_db
        from app.attestation_registry import init_registry_db
        from app.mygithub12 import init_db as init_mygithub12_db
        ensure_idempotency_storage()
        init_db()
        init_deployment_db()
        init_registry_db()
        init_mygithub12_db()
    except Exception:
        logger.exception("Controller database initialization failed")
        raise
    mcp_task = None
    tg = None
    try:
        from app.mcp_server import mcp
        mcp_sm = mcp._session_manager
        if mcp_sm is not None:
            import anyio
            tg = anyio.create_task_group()
            mcp_task = await tg.__aenter__()
            mcp_sm._task_group = mcp_task
            logger.info("MCP session manager initialized")
        try:
            tools = await mcp.list_tools()
            for t in tools:
                logger.info("Registered tool: %s", t.name)
            logger.info("Total registered tools: %d", len(tools))
        except Exception as e:
            logger.error("Failed to list registered tools during startup: %s", e)
    except Exception as e:
        logger.warning(f"MCP session init error: {e}")
    yield
    if mcp_task is not None and tg is not None:
        try:
            await tg.__aexit__(None, None, None)
        except Exception:
            pass


app = FastAPI(
    title=SERVICE_NAME,
    description="Private GitHub code write service for ChatGPT Custom GPT Actions and MCP",
    version=SERVICE_VERSION,
    lifespan=lifespan,
)

app.include_router(health.router, tags=["Health"])
app.include_router(github.router, tags=["GitHub"])
app.include_router(ci_worker.router, tags=["CI Worker"])
app.include_router(deployments.router, tags=["Deployments"])
app.include_router(ci_monitor.router, tags=["CI Monitor"])

app.include_router(index_v12.router, prefix="/internal/mygithub12", tags=["MyGithut12 Index"])

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return get_oauth_protected_resource_metadata()


app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RequestObservabilityMiddleware)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "message": exc.message, "details": exc.details},
    )


@app.get("/privacy")
async def privacy():
    return {
        "message": "This private service stores bounded operational metadata needed for idempotency, CI logs, deployment state, and audit evidence. It does not intentionally store GitHub credentials or full MyGithub10 request bodies in audit records.",
        "retention": "Retention is controlled by the operator's database, log, artifact, and idempotency policies.",
    }


_ACTIONS_SCHEMA = None


@app.get("/actions-openapi.json")
async def actions_openapi():
    global _ACTIONS_SCHEMA
    if _ACTIONS_SCHEMA is not None:
        return _ACTIONS_SCHEMA

    openapi_schema = {
        "openapi": "3.0.3",
        "info": {
            "title": "GitHub Action Service",
            "description": "Write and read code on GitHub repositories via API",
            "version": SERVICE_VERSION,
        },
        "servers": [
            {"url": "https://github.555044.xyz", "description": "Production server"},
        ],
        "security": [{"ActionApiKey": []}],
        "paths": {
            "/api/v1/github/file": {
                "get": {
                    "operationId": "getGithubFile",
                    "summary": "Get file content from a GitHub repository",
                    "description": "Read a file's content from a GitHub repository, with optional line range selection.",
                    "x-openai-isConsequential": False,
                    "parameters": [
                        {"name": "repository", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Repository in owner/repo format"},
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "File path in the repository"},
                        {"name": "ref", "in": "query", "required": False, "schema": {"type": "string", "default": ""}, "description": "Git reference (branch, tag, commit SHA). Defaults to repository default branch."},
                        {"name": "start_line", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1}, "description": "Start line number (1-indexed)"},
                        {"name": "end_line", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1}, "description": "End line number (1-indexed)"},
                    ],
                    "responses": {
                        "200": {"description": "File content retrieved successfully"},
                        "401": {"description": "Invalid or missing API key"},
                        "403": {"description": "Repository not allowed"},
                        "404": {"description": "File not found"},
                    },
                }
            },
            "/api/v1/github/directory": {
                "get": {
                    "operationId": "listGithubDirectory",
                    "summary": "List directory contents in a GitHub repository",
                    "description": "List files and subdirectories in a directory.",
                    "x-openai-isConsequential": False,
                    "parameters": [
                        {"name": "repository", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Repository in owner/repo format"},
                        {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Directory path in the repository"},
                        {"name": "ref", "in": "query", "required": False, "schema": {"type": "string", "default": ""}, "description": "Git reference. Defaults to repository default branch."},
                    ],
                    "responses": {
                        "200": {"description": "Directory contents retrieved"},
                        "401": {"description": "Invalid or missing API key"},
                        "403": {"description": "Repository not allowed"},
                        "404": {"description": "Directory not found"},
                    },
                }
            },
            "/api/v1/github/branches": {
                "post": {
                    "operationId": "createGithubBranch",
                    "summary": "Create a new branch in a GitHub repository",
                    "description": "Create a new branch from a base branch. Does not overwrite existing branches.",
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["repository", "branch"],
                                    "properties": {
                                        "repository": {"type": "string", "description": "Repository in owner/repo format"},
                                        "branch": {"type": "string", "description": "New branch name"},
                                        "base_branch": {"type": "string", "default": "main", "description": "Base branch to create from"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Branch created successfully"},
                        "401": {"description": "Invalid or missing API key"},
                        "403": {"description": "Repository not allowed"},
                        "409": {"description": "Branch already exists"},
                    },
                }
            },
            "/api/v1/github/commits": {
                "post": {
                    "operationId": "commitGithubFiles",
                    "summary": "Commit one or more files to a GitHub repository",
                    "description": "Create a Git commit with one or more file changes. All files go into a single commit. Supports upsert and delete operations.",
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["repository", "branch", "commit_message", "files"],
                                    "properties": {
                                        "repository": {"type": "string", "description": "Repository in owner/repo format"},
                                        "branch": {"type": "string", "description": "Target branch name"},
                                        "base_branch": {"type": "string", "default": "main", "description": "Base branch if creating a new branch"},
                                        "create_branch_if_missing": {"type": "boolean", "default": False, "description": "Create branch from base_branch if it doesn't exist"},
                                        "commit_message": {"type": "string", "description": "Git commit message"},
                                        "expected_head_sha": {"type": "string", "description": "Expected branch HEAD SHA for conflict detection"},
                                        "files": {
                                            "type": "array",
                                            "description": "Files to commit",
                                            "items": {
                                                "type": "object",
                                                "required": ["path", "operation"],
                                                "properties": {
                                                    "path": {"type": "string", "description": "File path in repository"},
                                                    "operation": {"type": "string", "enum": ["upsert", "delete"], "description": "Operation type"},
                                                    "content": {"type": "string", "description": "Full file content (required for upsert)"},
                                                    "expected_sha": {"type": "string", "description": "Expected file SHA for conflict detection"},
                                                },
                                            },
                                        },
                                        "pull_request": {
                                            "type": "object",
                                            "properties": {
                                                "create": {"type": "boolean", "default": False},
                                                "base_branch": {"type": "string", "default": "main"},
                                                "title": {"type": "string"},
                                                "body": {"type": "string"},
                                            },
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Files committed successfully"},
                        "400": {"description": "Invalid parameters"},
                        "401": {"description": "Invalid or missing API key"},
                        "403": {"description": "Repository not allowed or default branch write denied"},
                        "404": {"description": "Branch not found"},
                        "409": {"description": "SHA conflict or branch conflict"},
                        "413": {"description": "Content too large"},
                        "422": {"description": "Validation error"},
                    },
                }
            },
            "/api/v1/github/pull-requests": {
                "post": {
                    "operationId": "createGithubPullRequest",
                    "summary": "Create a pull request on GitHub",
                    "description": "Create a pull request between two branches.",
                    "x-openai-isConsequential": True,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["repository", "head_branch", "base_branch", "title"],
                                    "properties": {
                                        "repository": {"type": "string", "description": "Repository in owner/repo format"},
                                        "head_branch": {"type": "string", "description": "Source branch"},
                                        "base_branch": {"type": "string", "default": "main", "description": "Target branch"},
                                        "title": {"type": "string", "description": "Pull request title"},
                                        "body": {"type": "string", "default": "", "description": "Pull request description"},
                                        "draft": {"type": "boolean", "default": True, "description": "Create as draft PR"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Pull request created successfully"},
                        "401": {"description": "Invalid or missing API key"},
                        "403": {"description": "Repository not allowed"},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "ActionApiKey": {
                    "type": "http",
                    "scheme": "bearer",
                },
            },
        },
    }

    _ACTIONS_SCHEMA = openapi_schema
    return openapi_schema


try:
    from app.mcp_server import mcp
    mcp_app = mcp.streamable_http_app()
    app.mount("/", mcp_app)
    logger.info("MCP server mounted at / (endpoint: /mcp)")
    logger.info("MCP transport: Streamable HTTP (stateless)")
except Exception as e:
    logger.warning(f"MCP server not available: {e}")
