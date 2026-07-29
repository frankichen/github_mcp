import asyncio
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.exceptions import NotFoundError, ShaConflictError
from app.idempotency import IdempotencyMiddleware
from app.models import CommitRequest, FileOperation
from app.services.github_service import GitHubService


class CommitClient:
    def __init__(self, old_sha=None):
        self.old_sha = old_sha
        self.created_content = None

    def get_default_branch(self, _repository):
        return "main"

    def get_branch(self, _repository, _branch):
        return SimpleNamespace(commit=SimpleNamespace(sha="a" * 40))

    def get_file_sha(self, _repository, _path, _ref):
        return self.old_sha

    def create_blob(self, _repository, content):
        self.created_content = content
        return SimpleNamespace(sha="blob")

    def get_git_tree(self, _repository, _sha):
        return SimpleNamespace(sha="base-tree")

    def create_git_tree(self, _repository, _elements, _base_tree):
        return SimpleNamespace(sha="new-tree")

    def create_commit(self, _repository, message, tree_sha, parent_shas):
        return SimpleNamespace(sha="b" * 40)

    def update_ref(self, *_args, **_kwargs):
        return None

    def get_file(self, _repository, _path, _ref):
        return "", "blob", 0


def test_legacy_commit_supports_empty_files(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/repo")
    client = CommitClient()
    service = GitHubService(client)
    result = service.commit_files(
        CommitRequest(
            repository="owner/repo",
            branch="feature/empty",
            commit_message="add empty file",
            files=[FileOperation(path="empty.txt", operation="upsert", content="")],
        )
    )
    assert result["success"] is True
    assert client.created_content == ""
    assert result["changed_files"][0]["size_bytes"] == 0


def test_legacy_commit_expected_sha_conflicts_when_target_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/repo")
    service = GitHubService(CommitClient(old_sha=None))
    with pytest.raises(ShaConflictError):
        service.commit_files(
            CommitRequest(
                repository="owner/repo",
                branch="feature/cas",
                commit_message="strict cas",
                files=[
                    FileOperation(
                        path="missing.txt",
                        operation="upsert",
                        content="new",
                        expected_sha="expected-existing-blob",
                    )
                ],
            )
        )


def test_legacy_commit_rejects_deleting_a_missing_file(monkeypatch):
    monkeypatch.setattr(settings, "ALLOWED_REPOSITORIES", "owner/repo")
    service = GitHubService(CommitClient(old_sha=None))
    with pytest.raises(NotFoundError):
        service.commit_files(
            CommitRequest(
                repository="owner/repo",
                branch="feature/delete",
                commit_message="delete",
                files=[FileOperation(path="missing.txt", operation="delete")],
            )
        )


def _idempotency_app(db_path, handler):
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/mutate")
    async def mutate():
        return await handler()

    return app


def test_idempotency_does_not_cache_server_errors(tmp_path, monkeypatch):
    calls = 0
    db_path = tmp_path / "idempotency.db"
    monkeypatch.setattr(settings, "IDEMPOTENCY_DB_PATH", str(db_path))

    async def handler():
        nonlocal calls
        calls += 1
        if calls == 1:
            return JSONResponse({"ok": False}, status_code=503)
        return {"ok": True}

    with TestClient(_idempotency_app(db_path, handler)) as client:
        headers = {"Idempotency-Key": "retryable-request"}
        assert client.post("/mutate", headers=headers).status_code == 503
        assert client.post("/mutate", headers=headers).status_code == 200
    assert calls == 2


def test_concurrent_idempotency_key_has_one_owner(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = 0
    db_path = tmp_path / "idempotency.db"
    monkeypatch.setattr(settings, "IDEMPOTENCY_DB_PATH", str(db_path))

    async def handler():
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return {"ok": True}

    with TestClient(_idempotency_app(db_path, handler)) as client:
        first = {}

        def run_first():
            first["response"] = client.post(
                "/mutate", headers={"Idempotency-Key": "concurrent-request"}
            )

        thread = threading.Thread(target=run_first)
        thread.start()
        assert started.wait(1)
        second = client.post(
            "/mutate", headers={"Idempotency-Key": "concurrent-request"}
        )
        release.set()
        thread.join(timeout=2)

    assert first["response"].status_code == 200
    assert second.status_code == 409
    assert second.json()["error"] == "idempotency_in_progress"
    assert calls == 1
