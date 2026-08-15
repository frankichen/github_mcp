import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from fastapi.testclient import TestClient

import os
os.environ["GITHUB_TOKEN"] = "test_token_value"
os.environ["ACTION_API_KEY"] = "test_api_key_32_bytes_long"
os.environ["ALLOWED_REPOSITORIES"] = "owner/allowed-repo"
os.environ["ALLOW_DEFAULT_BRANCH_WRITE"] = "false"
os.environ["MAX_FILE_CHARACTERS"] = "5000"
os.environ["MAX_TOTAL_CHARACTERS"] = "10000"
os.environ["MAX_FILES_PER_COMMIT"] = "5"

from app.main import app
from app.config import settings

VALID_API_KEY = settings.ACTION_API_KEY.get_secret_value()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def auth_headers():
    return {"Authorization": f"Bearer {VALID_API_KEY}"}


def get_error(data):
    return data.get("detail", data)


class TestAuth:
    def test_no_api_key_returns_401(self, client):
        response = client.get(
            "/api/v1/github/file",
            params={"repository": "owner/repo", "path": "README.md"},
        )
        assert response.status_code == 401
        data = get_error(response.json())
        assert data["error"] == "unauthorized"

    def test_wrong_api_key_returns_401(self, client):
        response = client.get(
            "/api/v1/github/file",
            params={"repository": "owner/repo", "path": "README.md"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401
        data = get_error(response.json())
        assert data["error"] == "unauthorized"

    def test_correct_api_key_allowed(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_repo.get_contents.return_value = MagicMock(
                decoded_content=b"Hello World",
                sha="abc123",
                size=11,
            )
            mock_gh.get_repo.return_value = mock_repo

            response = client.get(
                "/api/v1/github/file",
                params={"repository": "owner/allowed-repo", "path": "README.md"},
                headers=auth_headers(),
            )
            assert response.status_code == 200

    def test_health_no_auth_required(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_actions_openapi_no_auth(self, client):
        response = client.get("/actions-openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "paths" in data
        assert "/api/v1/github/file" in data["paths"]

    def test_privacy_no_auth(self, client):
        response = client.get("/privacy")
        assert response.status_code == 200
        assert "stores bounded operational metadata" in response.json()["message"]

    def test_readiness_reports_initialized_dependencies(self, client):
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_metrics_requires_auth_and_exposes_bounded_routes(self, client):
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers=auth_headers())
        assert response.status_code == 200
        assert "mygithub_http_requests_total" in response.text


class TestRepositoryAccess:
    def test_non_whitelisted_repo_returns_403(self, client):
        with patch.object(settings, "ALLOWED_REPOSITORIES", "owner/allowed-repo"):
            response = client.get(
                "/api/v1/github/file",
                params={"repository": "other/repo", "path": "README.md"},
                headers=auth_headers(),
            )
            assert response.status_code == 403
            data = get_error(response.json())
            assert data["error"] == "repository_not_allowed"

    def test_whitelisted_repo_allowed(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_repo.get_contents.return_value = MagicMock(
                decoded_content=b"hello",
                sha="abc",
                size=5,
            )
            mock_gh.get_repo.return_value = mock_repo

            response = client.get(
                "/api/v1/github/file",
                params={"repository": "owner/allowed-repo", "path": "test.txt"},
                headers=auth_headers(),
            )
            assert response.status_code == 200


class TestPathValidation:
    def test_empty_path(self, client):
        response = client.get(
            "/api/v1/github/file",
            params={"repository": "owner/allowed-repo", "path": ""},
            headers=auth_headers(),
        )
        assert response.status_code == 422
        data = get_error(response.json())
        assert data["error"] == "validation_error"

    def test_path_with_dotdot(self, client):
        response = client.get(
            "/api/v1/github/file",
            params={"repository": "owner/allowed-repo", "path": "a/../b"},
            headers=auth_headers(),
        )
        assert response.status_code == 422

    def test_path_starting_with_slash(self, client):
        response = client.get(
            "/api/v1/github/file",
            params={"repository": "owner/allowed-repo", "path": "/etc/passwd"},
            headers=auth_headers(),
        )
        assert response.status_code == 422


class TestContentLimits:
    def test_single_file_too_large(self, client):
        with patch.object(settings, "MAX_FILE_CHARACTERS", 600):
            with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
                mock_repo = MagicMock()
                mock_repo.default_branch = "main"
                mock_gh.get_repo.return_value = mock_repo

                big_content = "x" * 1000
                response = client.post(
                    "/api/v1/github/commits",
                    json={
                        "repository": "owner/allowed-repo",
                        "branch": "ai/test",
                        "commit_message": "test",
                        "create_branch_if_missing": True,
                        "files": [
                            {"path": "big.txt", "operation": "upsert", "content": big_content}
                        ],
                    },
                    headers=auth_headers(),
                )
                assert response.status_code == 413
                data = get_error(response.json())
                assert data["error"] == "content_too_large"

    def test_total_content_too_large(self, client):
        with patch.object(settings, "MAX_TOTAL_CHARACTERS", 200):
            with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
                mock_repo = MagicMock()
                mock_repo.default_branch = "main"
                mock_gh.get_repo.return_value = mock_repo

                response = client.post(
                    "/api/v1/github/commits",
                    json={
                        "repository": "owner/allowed-repo",
                        "branch": "ai/test",
                        "commit_message": "test",
                        "create_branch_if_missing": True,
                        "files": [
                            {"path": "a.txt", "operation": "upsert", "content": "x" * 100},
                            {"path": "b.txt", "operation": "upsert", "content": "x" * 100},
                            {"path": "c.txt", "operation": "upsert", "content": "x" * 100},
                        ],
                    },
                    headers=auth_headers(),
                )
                assert response.status_code == 413

    def test_too_many_files(self, client):
        with patch.object(settings, "MAX_FILES_PER_COMMIT", 3):
            with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
                mock_repo = MagicMock()
                mock_repo.default_branch = "main"
                mock_gh.get_repo.return_value = mock_repo

                files = [
                    {"path": f"file_{i}.txt", "operation": "upsert", "content": "hello"}
                    for i in range(10)
                ]
                response = client.post(
                    "/api/v1/github/commits",
                    json={
                        "repository": "owner/allowed-repo",
                        "branch": "ai/test",
                        "commit_message": "test",
                        "create_branch_if_missing": True,
                        "files": files,
                    },
                    headers=auth_headers(),
                )
                assert response.status_code == 413


class TestDefaultBranchWrite:
    def test_default_branch_write_denied(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/commits",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "main",
                    "commit_message": "test",
                    "files": [
                        {"path": "test.txt", "operation": "upsert", "content": "hello"}
                    ],
                },
                headers=auth_headers(),
            )
            assert response.status_code == 403
            data = get_error(response.json())
            assert data["error"] == "default_branch_write_denied"


class TestSHAConflicts:
    def test_expected_sha_conflict(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"

            mock_branch = MagicMock()
            mock_branch.commit.sha = "head_sha_123"
            mock_repo.get_branch.return_value = mock_branch

            mock_contents = MagicMock()
            mock_contents.sha = "file_sha_different"
            mock_repo.get_contents.return_value = mock_contents

            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/commits",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "feature/test",
                    "commit_message": "test",
                    "files": [
                        {
                            "path": "test.txt",
                            "operation": "upsert",
                            "content": "hello",
                            "expected_sha": "file_sha_expected",
                        }
                    ],
                },
                headers=auth_headers(),
            )
            assert response.status_code == 409
            data = get_error(response.json())
            assert data["error"] == "sha_conflict"

    def test_expected_head_sha_conflict(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"

            mock_branch = MagicMock()
            mock_branch.commit.sha = "current_head_sha"
            mock_repo.get_branch.return_value = mock_branch

            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/commits",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "feature/test",
                    "commit_message": "test",
                    "expected_head_sha": "different_head_sha",
                    "files": [
                        {"path": "test.txt", "operation": "upsert", "content": "hello"}
                    ],
                },
                headers=auth_headers(),
            )
            assert response.status_code == 409
            data = get_error(response.json())
            assert data["error"] == "head_sha_conflict"


class TestMultiFileCommit:
    def test_multiple_files_single_commit(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"

            mock_branch = MagicMock()
            mock_branch.commit.sha = "parent_sha"
            mock_repo.get_branch.return_value = mock_branch

            mock_blob1 = MagicMock()
            mock_blob1.sha = "blob1_sha"
            mock_blob2 = MagicMock()
            mock_blob2.sha = "blob2_sha"
            mock_repo.create_git_blob.side_effect = [mock_blob1, mock_blob2]

            mock_tree = MagicMock()
            mock_tree.sha = "tree_sha"
            mock_repo.get_git_tree.return_value = mock_tree
            mock_repo.create_git_tree.return_value = mock_tree

            mock_commit = MagicMock()
            mock_commit.sha = "commit_sha_123"
            mock_repo.create_git_commit.return_value = mock_commit

            mock_ref = MagicMock()
            mock_repo.get_git_ref.return_value = mock_ref

            readback_a = MagicMock(sha="blob1_sha", decoded_content=b"content a")
            readback_b = MagicMock(sha="blob2_sha", decoded_content=b"content b")
            mock_repo.get_contents.side_effect = [
                MagicMock(sha="old_a"), MagicMock(sha="old_b"), readback_a, readback_b,
            ]

            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/commits",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "feature/test",
                    "commit_message": "feat: multi-file commit",
                    "files": [
                        {"path": "a.txt", "operation": "upsert", "content": "content a"},
                        {"path": "b.txt", "operation": "upsert", "content": "content b"},
                    ],
                },
                headers=auth_headers(),
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["commit_sha"] == "commit_sha_123"
            assert len(data["changed_files"]) == 2

            assert mock_repo.create_git_commit.call_count == 1
            assert mock_repo.create_git_blob.call_count == 2


class TestTokenLeak:
    def test_error_response_no_token(self, client):
        with patch.object(settings, "ALLOWED_REPOSITORIES", "owner/allowed-repo"):
            response = client.get(
                "/api/v1/github/file",
                params={"repository": "other/repo", "path": "README.md"},
                headers=auth_headers(),
            )
            assert response.status_code == 403
            response_text = response.text
            assert "test_token_value" not in response_text

    def test_health_no_token(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        response_text = response.text
        assert "test_token_value" not in response_text
        assert VALID_API_KEY not in response_text


class TestGitHubErrorMapping:
    def test_branch_not_found_maps_to_404(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_repo.get_branch.return_value = None
            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/commits",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "nonexistent",
                    "commit_message": "test",
                    "files": [
                        {"path": "test.txt", "operation": "upsert", "content": "hello"}
                    ],
                },
                headers=auth_headers(),
            )
            assert response.status_code == 404

    def test_create_branch_accepts_commit_sha_base_ref(self, client):
        base_sha = "a" * 40
        branch = "ai/sha-base"
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_repo.get_branch.side_effect = lambda name: None if name == branch else (_ for _ in ()).throw(AssertionError("base ref must not be resolved as a branch"))
            mock_repo.get_commit.return_value = MagicMock(sha=base_sha)
            mock_repo.create_git_ref.return_value = MagicMock(object=MagicMock(sha=base_sha))
            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/branches",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": branch,
                    "base_branch": base_sha,
                },
                headers=auth_headers(),
            )

            assert response.status_code == 200
            assert response.json()["commit_sha"] == base_sha
            mock_repo.get_commit.assert_called_once_with(base_sha)
            mock_repo.create_git_ref.assert_called_once_with(f"refs/heads/{branch}", base_sha)

    def test_branch_exists_maps_to_409(self, client):
        with patch("app.routers.github._client._pygithub", create=True) as mock_gh:
            mock_repo = MagicMock()
            mock_repo.default_branch = "main"
            mock_branch = MagicMock()
            mock_repo.get_branch.return_value = mock_branch
            mock_gh.get_repo.return_value = mock_repo

            response = client.post(
                "/api/v1/github/branches",
                json={
                    "repository": "owner/allowed-repo",
                    "branch": "existing-branch",
                    "base_branch": "main",
                },
                headers=auth_headers(),
            )
            assert response.status_code == 409
            data = get_error(response.json())
            assert data["error"] == "branch_exists"
