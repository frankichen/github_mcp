from fastapi import APIRouter
from app.github_client import GitHubClient

router = APIRouter()

_client = GitHubClient()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "github-action-service",
        "github_configured": _client.configured,
    }
