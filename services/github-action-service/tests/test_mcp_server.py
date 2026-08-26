import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient

import os
os.environ["GITHUB_TOKEN"] = "test_token_value"
os.environ["ACTION_API_KEY"] = "test_api_key_32_bytes_long"
os.environ["ALLOWED_REPOSITORIES"] = "owner/allowed-repo"
os.environ["ALLOW_DEFAULT_BRANCH_WRITE"] = "false"
os.environ["MAX_FILE_CHARACTERS"] = "5000"
os.environ["MAX_TOTAL_CHARACTERS"] = "10000"
os.environ["MAX_FILES_PER_COMMIT"] = "5"
os.environ["SERVICE_URL"] = "https://github.555044.xyz"
os.environ["IDEMPOTENCY_DB_PATH"] = "/tmp/idempotency_test.db"

from app.main import app
from app.config import settings

VALID_API_KEY = settings.ACTION_API_KEY.get_secret_value()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def auth_headers():
    return {"Authorization": f"Bearer {VALID_API_KEY}"}


class TestMCPTools:
    @pytest.mark.asyncio
    async def test_mcp_transport_security_uses_explicit_public_host(self):
        from app.mcp_server import mcp

        from mcp.server.transport_security import TransportSecurityMiddleware

        security = mcp.settings.transport_security
        middleware = TransportSecurityMiddleware(security)
        assert security.enable_dns_rebinding_protection is True
        assert "github.555044.xyz" in security.allowed_hosts
        assert "*" not in security.allowed_hosts
        assert middleware._validate_host("github.555044.xyz") is True
        assert middleware._validate_host("evil.example") is False
        assert middleware._validate_host("github.555044.xyz:443") is True
        assert middleware._validate_origin("https://github.555044.xyz") is True
        assert middleware._validate_origin("https://evil.example") is False

    @pytest.mark.asyncio
    async def test_mygithub10_schema_and_gofmt_contract(self, monkeypatch):
        from app.mcp_server import mcp

        monkeypatch.setenv("MYGITHUB12_EXPOSE_DEPRECATED_TOOLS", "true")
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        for name in ("build_github_patch", "replace_github_text_once", "edit_github_file_ranges", "apply_github_patch", "get_mygithub_capabilities", "plan_private_ci_job"):
            assert name in tools
        assert "expected_blob_sha" in tools["edit_github_file_ranges"].description
        assert "expected_old_text" in tools["edit_github_file_ranges"].description
        assert "replacement_text" in tools["edit_github_file_ranges"].description
        exact_schema = tools["replace_github_text_once"].inputSchema["properties"]
        assert "start_line" not in exact_schema
        assert "end_line" not in exact_schema
        assert exact_schema["expected_match_count"]["default"] == 1
        assert "without caller-supplied line numbers" in tools["replace_github_text_once"].description
        assert "start_line" not in tools["build_github_patch"].description
        operations_schema = tools["edit_github_file_ranges"].inputSchema["properties"]["operations_json"]
        for field in (
            "expected_blob_sha",
            "expected_old_text",
            "expected_old_text_sha256",
            "replacement_text",
        ):
            assert field in operations_schema["description"]
        annotations = tools["build_github_patch"].annotations
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False
        assert annotations.idempotentHint is True
        assert annotations.openWorldHint is False
        import json
        from app.mcp_server import get_mygithub_capabilities
        capabilities = json.loads(await get_mygithub_capabilities())
        assert capabilities["name"] == "MyGithut12"
        assert capabilities["version"] == "12.2.1"
        assert capabilities["max_upload_chunk_bytes"] == 24576
        assert capabilities["recommended_upload_chunk_bytes"] == 16384
        assert capabilities["preferred_upload_encoding"] == "text_for_utf8_base64_for_binary"
        assert capabilities["tool_count"] == 163
        assert capabilities["tool_manifest_count"] == 163
        assert capabilities["compatibility_tool_count"] == 163
        assert capabilities["deprecated_tools_exposed"] is True
        assert capabilities["hidden_deprecated_tool_count"] == 0
        assert capabilities["hidden_deprecated_tools"] == []
        assert len(capabilities["tool_schema_sha256"]) == 64
        assert capabilities["schema_generation_id"].startswith("schema-v1:")
        assert capabilities["supports_exact_text_replace"] is True
        assert capabilities["supports_atomic_multi_upload_change_set"] is True
        assert capabilities["max_atomic_multi_upload_files"] == 64
        assert capabilities["max_atomic_multi_upload_bytes"] == 64 * 1024 * 1024
        assert capabilities["supports_private_ci_applicability_planning"] is True
        assert capabilities["supports_development_task_orchestration"] is True
        assert capabilities["supports_development_sessions"] is True
        assert capabilities["supports_local_git_mirror_reads"] is True
        assert capabilities["supports_context_pack_v2"] is True
        assert capabilities["supports_blue_green_runtime"] is True
        assert capabilities["max_inline_response_bytes"] == 32768
        assert capabilities["transport_inline_hard_limit_bytes"] == 65536
        assert capabilities["supports_structured_content"] is True
        assert capabilities["supports_response_resource_fallback"] is True
        assert capabilities["supports_repository_text_search"] is True
        assert capabilities["supports_development_workspaces"] is True
        assert capabilities["supports_workspace_revision_cas"] is True
        assert len(capabilities["build_sha"]) == 40
        assert capabilities["supports_gofmt_autofix"] is False
        assert capabilities["supports_gofmt_readonly_check"] is True

    @pytest.mark.asyncio
    async def test_mcp_tools_are_registered(self):
        from app.mcp_server import mcp
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "get_github_file" in tool_names
        assert "list_github_directory" in tool_names
        assert "create_github_branch" in tool_names
        assert "commit_github_files" in tool_names
        assert "create_github_pull_request" in tool_names

    @pytest.mark.asyncio
    async def test_mcp_tool_set_and_key_tools(self):
        from app.mcp_server import mcp
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert len(tool_names) == len(set(tool_names))
        for name in (
            "get_github_pull_request_merge_readiness", "merge_github_pull_request",
            "plan_test_deployment", "start_test_deployment", "rollback_test_deployment",
            "start_private_ci_job", "wait_test_deployment",
        ):
            assert name in tool_names

    @pytest.mark.asyncio
    async def test_private_ci_start_schema_has_no_command_or_image_injection(self):
        from app.mcp_server import mcp

        tools = {tool.name: tool for tool in await mcp.list_tools()}
        properties = tools["start_private_ci_job"].inputSchema["properties"]
        assert {"repository", "branch", "commit_sha", "profile"}.issubset(properties)
        assert "command" not in properties
        assert "image" not in properties

    @pytest.mark.asyncio
    async def test_get_github_file_tool(self):
        import json
        from app.mcp_server import get_github_file

        with patch("app.mcp_server._service") as mock_service:
            mock_service.get_file.return_value = {
                "repository": "owner/repo",
                "path": "test.py",
                "ref": "main",
                "sha": "abc123",
                "size": 100,
                "content": "print('hello')",
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "truncated": False,
            }

            result = await get_github_file(repository="owner/repo", path="test.py", ref="main")

            data = json.loads(result)
            assert data["sha"] == "abc123"
            assert data["content"] == "print('hello')"
            mock_service.get_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_github_directory_tool(self):
        import json
        from app.mcp_server import list_github_directory

        with patch("app.mcp_server._service") as mock_service:
            mock_service.list_directory.return_value = {
                "repository": "owner/repo",
                "path": "src",
                "ref": "main",
                "items": [
                    {"name": "file.py", "path": "src/file.py", "type": "file", "sha": "abc", "size": 42},
                ],
            }

            result = await list_github_directory(repository="owner/repo", path="src")

            data = json.loads(result)
            assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_create_github_branch_tool(self):
        import json
        from app.mcp_server import create_github_branch

        with patch("app.mcp_server._service") as mock_service:
            mock_service.create_branch.return_value = {
                "success": True,
                "repository": "owner/repo",
                "branch": "feature-x",
                "base_branch": "main",
                "commit_sha": "abc123",
            }

            result = await create_github_branch(repository="owner/repo", branch="feature-x")

            data = json.loads(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_commit_github_files_tool(self):
        import json
        from app.mcp_server import commit_github_files

        files_json = json.dumps([{"path": "test.py", "operation": "upsert", "content": "print('hello')"}])

        with patch("app.mcp_server._service") as mock_service:
            mock_service.commit_files.return_value = {
                "success": True,
                "repository": "owner/repo",
                "branch": "feature-x",
                "commit_sha": "abc123",
                "new_head_sha": "abc123",
                "old_head_sha": "old123",
                "tree_sha": "tree123",
                "commit_url": "https://github.com/owner/repo/commit/abc123",
                "changed_files": [{"path": "test.py", "operation": "upsert"}],
                "pull_request": None,
                "write_verified": True,
                "previous_head_sha": "old123",
                "verified_branch_head_sha": "abc123",
                "verified_commit_sha": "abc123",
                "verified_tree_sha": "tree123",
            }

            result = await commit_github_files(
                repository="owner/repo",
                branch="feature-x",
                commit_message="test commit",
                files_json=files_json,
            )

            data = json.loads(result)
            assert data["success"] is True
            assert data["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    async def test_create_github_pull_request_tool(self):
        import json
        from app.mcp_server import create_github_pull_request

        with patch("app.mcp_server._service") as mock_service:
            mock_service.create_pull_request.return_value = {
                "success": True,
                "repository": "owner/repo",
                "head_branch": "feature-x",
                "base_branch": "main",
                "pull_request": {"number": 42, "url": "https://github.com/owner/repo/pull/42"},
            }

            result = await create_github_pull_request(
                repository="owner/repo",
                head_branch="feature-x",
                base_branch="main",
                title="Test PR",
            )

            data = json.loads(result)
            assert data["success"] is True
            assert data["pull_request"]["number"] == 42

    @pytest.mark.asyncio
    async def test_tool_returns_error_on_exception(self):
        import json
        from app.mcp_server import get_github_file
        from app.exceptions import NotFoundError

        with patch("app.mcp_server._service") as mock_service:
            mock_service.get_file.side_effect = NotFoundError("File not found")

            result = await get_github_file(repository="owner/repo", path="nonexistent.py")

            data = json.loads(result)
            assert data["error"] == "NotFoundError"

    @pytest.mark.asyncio
    async def test_token_verifier_valid(self):
        from app.mcp_server import ApiKeyVerifier

        verifier = ApiKeyVerifier()
        result = await verifier.verify_token(VALID_API_KEY)
        assert result is not None
        assert result.client_id == "github-action-service"

    @pytest.mark.asyncio
    async def test_token_verifier_invalid(self):
        from app.mcp_server import ApiKeyVerifier

        verifier = ApiKeyVerifier()
        result = await verifier.verify_token("wrong-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_token_verifier_empty(self):
        from app.mcp_server import ApiKeyVerifier

        verifier = ApiKeyVerifier()
        result = await verifier.verify_token("")
        assert result is None


class TestCITools:
    @pytest.mark.asyncio
    async def test_ci_tools_are_registered(self):
        from app.mcp_server import mcp
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "list_ci_workers" in tool_names
        assert "list_ci_profiles" in tool_names
        assert "list_ci_jobs" in tool_names
        assert "start_ci_job" in tool_names
        assert "get_ci_job" in tool_names
        assert "get_ci_logs" in tool_names
        assert "cancel_ci_job" in tool_names

    @pytest.mark.asyncio
    async def test_list_ci_workers_tool(self):
        import json
        from app.mcp_server import list_ci_workers

        mock_result = {
            "repository": "owner/repo",
            "total_count": 2,
            "runners": [
                {"id": 1, "name": "runner-1", "os": "linux", "status": "online", "busy": False, "labels": ["self-hosted"]},
                {"id": 2, "name": "runner-2", "os": "linux", "status": "offline", "busy": False, "labels": ["self-hosted"]},
            ],
        }

        with patch("app.mcp_server._ci_service.list_ci_workers", AsyncMock(return_value=mock_result)):
            result = await list_ci_workers(repository="owner/repo")
            data = json.loads(result)
            assert data["total_count"] == 2
            assert len(data["runners"]) == 2

    @pytest.mark.asyncio
    async def test_list_ci_profiles_tool(self):
        import json
        from app.mcp_server import list_ci_profiles

        mock_result = {
            "repository": "owner/repo",
            "total_count": 1,
            "workflows": [
                {"id": 123, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active", "url": "https://github.com/owner/repo/actions/workflows/ci.yml"},
            ],
        }

        with patch("app.mcp_server._ci_service.list_ci_profiles", AsyncMock(return_value=mock_result)):
            result = await list_ci_profiles(repository="owner/repo")
            data = json.loads(result)
            assert data["total_count"] == 1
            assert data["workflows"][0]["name"] == "CI"

    @pytest.mark.asyncio
    async def test_list_ci_jobs_tool(self):
        import json
        from app.mcp_server import list_ci_jobs

        mock_result = {
            "repository": "owner/repo",
            "total_count": 1,
            "workflow_runs": [
                {"id": 456, "name": "CI", "status": "completed", "conclusion": "success", "head_branch": "main", "url": "https://github.com/owner/repo/actions/runs/456"},
            ],
        }

        with patch("app.mcp_server._ci_service.list_ci_jobs", AsyncMock(return_value=mock_result)):
            result = await list_ci_jobs(repository="owner/repo")
            data = json.loads(result)
            assert data["total_count"] == 1
            assert data["workflow_runs"][0]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_ci_job_tool(self):
        import json
        from app.mcp_server import get_ci_job

        mock_result = {
            "repository": "owner/repo",
            "run_id": 456,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "jobs": [{"id": 1, "name": "build", "status": "completed", "conclusion": "success", "steps": []}],
        }

        with patch("app.mcp_server._ci_service.get_ci_job", AsyncMock(return_value=mock_result)):
            result = await get_ci_job(repository="owner/repo", run_id="456")
            data = json.loads(result)
            assert data["status"] == "completed"
            assert len(data["jobs"]) == 1

    @pytest.mark.asyncio
    async def test_get_ci_logs_tool(self):
        import json
        from app.mcp_server import get_ci_logs

        mock_result = {
            "repository": "owner/repo",
            "run_id": "456",
            "logs": [{"job_id": 1, "log": "Build output..."}],
        }

        with patch("app.mcp_server._ci_service.get_ci_logs", AsyncMock(return_value=mock_result)):
            result = await get_ci_logs(repository="owner/repo", run_id="456")
            data = json.loads(result)
            assert data["run_id"] == "456"
            assert len(data["logs"]) == 1

    @pytest.mark.asyncio
    async def test_start_ci_job_tool(self):
        import json
        from app.mcp_server import start_ci_job

        mock_result = {
            "success": True,
            "repository": "owner/repo",
            "workflow_id": "123",
            "ref": "main",
            "message": "Workflow dispatch triggered",
        }

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(return_value=mock_result)):
            result = await start_ci_job(repository="owner/repo", workflow_id="123", ref="main")
            data = json.loads(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_cancel_ci_job_tool(self):
        import json
        from app.mcp_server import cancel_ci_job

        mock_result = {
            "success": True,
            "repository": "owner/repo",
            "run_id": "456",
            "message": "Workflow run 456 cancelled successfully",
        }

        with patch("app.mcp_server._ci_service.cancel_ci_job", AsyncMock(return_value=mock_result)):
            result = await cancel_ci_job(repository="owner/repo", run_id="456")
            data = json.loads(result)
            assert data["success"] is True

    @pytest.mark.asyncio
    async def test_ci_tool_error_handling(self):
        import json
        from app.mcp_server import list_ci_workers

        with patch("app.mcp_server._ci_service.list_ci_workers", AsyncMock(side_effect=Exception("API connection failed"))):
            result = await list_ci_workers(repository="owner/repo")
            data = json.loads(result)
            assert "error" in data
            assert data["error"] == "Exception"
            assert "API connection failed" in data["message"]


class TestStartCiJob:
    @pytest.mark.asyncio
    async def test_start_ci_job_success_empty_inputs(self):
        """Test 1: inputs_json='{}' + GitHub returns 204 → success=true"""
        import json
        from app.mcp_server import start_ci_job

        mock_result = {
            "success": True,
            "repository": "owner/repo",
            "workflow_id": "ci.yml",
            "ref": "main",
            "message": "Workflow dispatch triggered for workflow ci.yml on ref main",
        }

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(return_value=mock_result)):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml", inputs_json="{}")
            data = json.loads(result)
            assert data["success"] is True
            assert "error" not in data

    @pytest.mark.asyncio
    async def test_start_ci_job_success_with_inputs(self):
        """Test 2: Valid inputs_json with inputs → success=true, and service receives inputs"""
        import json
        from app.mcp_server import start_ci_job

        mock_result = {
            "success": True,
            "repository": "owner/repo",
            "workflow_id": "ci.yml",
            "ref": "main",
            "message": "Workflow dispatch triggered for workflow ci.yml on ref main",
        }

        mock_fn = AsyncMock(return_value=mock_result)
        with patch("app.mcp_server._ci_service.start_ci_job", mock_fn):
            result = await start_ci_job(
                repository="owner/repo",
                workflow_id="ci.yml",
                ref="main",
                inputs_json='{"environment":"test"}',
            )
            data = json.loads(result)
            assert data["success"] is True
            mock_fn.assert_called_once_with(
                repository="owner/repo",
                workflow_id="ci.yml",
                ref="main",
                inputs={"environment": "test"},
            )

    @pytest.mark.asyncio
    async def test_start_ci_job_invalid_json(self):
        """Test 3: inputs_json='{' → json_parse_error, no HTTP call"""
        import json
        from app.mcp_server import start_ci_job

        mock_fn = AsyncMock()
        with patch("app.mcp_server._ci_service.start_ci_job", mock_fn):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml", inputs_json="{")
            data = json.loads(result)
            assert data["error"] == "json_parse_error"
            assert "not valid JSON" in data["message"]
            mock_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_ci_job_array_inputs(self):
        """Test 4: inputs_json='[]' → validation_error, no HTTP call"""
        import json
        from app.mcp_server import start_ci_job

        mock_fn = AsyncMock()
        with patch("app.mcp_server._ci_service.start_ci_job", mock_fn):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml", inputs_json="[]")
            data = json.loads(result)
            assert data["error"] == "validation_error"
            assert "JSON object" in data["message"]
            mock_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_ci_job_http_422(self):
        """Test 5: GitHub 422 → error includes GitHub response body, not 'inputs_json is not valid'"""
        import json
        from app.mcp_server import start_ci_job

        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = '{"message":"Workflow does not have workflow_dispatch trigger"}'
        import httpx
        http_error = httpx.HTTPStatusError("422 error", request=MagicMock(), response=mock_response)

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=http_error)):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "http_error"
            assert data["status_code"] == 422
            assert "inputs_json" not in data["message"].lower()
            assert "Workflow does not have" in data["message"]

    @pytest.mark.asyncio
    async def test_start_ci_job_http_404(self):
        """Test 6: GitHub 404 → workflow or repo not found error"""
        import json
        from app.mcp_server import start_ci_job

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = '{"message":"Not Found"}'
        import httpx
        http_error = httpx.HTTPStatusError("404 error", request=MagicMock(), response=mock_response)

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=http_error)):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "http_error"
            assert data["status_code"] == 404
            assert "inputs_json" not in data["message"].lower()

    @pytest.mark.asyncio
    async def test_start_ci_job_http_401(self):
        """Test 7: GitHub 401 → auth error, not 'inputs_json is not valid'"""
        import json
        from app.mcp_server import start_ci_job

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = '{"message":"Bad credentials"}'
        import httpx
        http_error = httpx.HTTPStatusError("401 error", request=MagicMock(), response=mock_response)

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=http_error)):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "http_error"
            assert data["status_code"] == 401
            assert "inputs_json" not in data["message"].lower()

    @pytest.mark.asyncio
    async def test_start_ci_job_timeout(self):
        """Test 8: Network timeout → timeout error"""
        import json
        from app.mcp_server import start_ci_job
        import httpx

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "timeout"
            assert "timed out" in data["message"]

    @pytest.mark.asyncio
    async def test_start_ci_job_generic_exception(self):
        """Test 9: Non-HTTP exception → generic error preserved"""
        import json
        from app.mcp_server import start_ci_job

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=ValueError("bad repo"))):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "ValueError"
            assert data["message"] == "bad repo"

    @pytest.mark.asyncio
    async def test_start_ci_job_http_403(self):
        """Test 10: GitHub 403 → permission error, not 'inputs_json is not valid'"""
        import json
        from app.mcp_server import start_ci_job

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = '{"message":"Resource not accessible by integration"}'
        import httpx
        http_error = httpx.HTTPStatusError("403 error", request=MagicMock(), response=mock_response)

        with patch("app.mcp_server._ci_service.start_ci_job", AsyncMock(side_effect=http_error)):
            result = await start_ci_job(repository="owner/repo", workflow_id="ci.yml")
            data = json.loads(result)
            assert data["error"] == "http_error"
            assert data["status_code"] == 403
            assert "inputs_json" not in data["message"].lower()


class TestOAuthWellKnown:
    def test_well_known_returns_200(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200

    def test_well_known_resource_field(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        data = response.json()
        assert data["resource"] == "https://github.555044.xyz"

    def test_well_known_authorization_servers(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        data = response.json()
        assert "https://github.555044.xyz" in data["authorization_servers"]

    def test_well_known_bearer_methods(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        data = response.json()
        assert "header" in data["bearer_methods_supported"]

    def test_well_known_no_auth_required(self, client):
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200


class TestMergeIndexBootstrap:
    @pytest.mark.asyncio
    async def test_confirmed_merge_queues_exact_new_base_index(self, monkeypatch):
        import json
        import app.mcp_server as mcp_server

        old_base = "b" * 40
        new_base = "c" * 40
        merged = {
            "ok": True,
            "merged": True,
            "repository": "owner/allowed-repo",
            "pull_number": 7,
            "base_head_before": old_base,
            "base_head_after": new_base,
            "merge_commit_sha": new_base,
        }
        calls = []

        async def fake_github_call(function, *args):
            if function is mcp_server.github_utils.merge_github_pull_request:
                return dict(merged)
            if function is mcp_server.mygithub12.request_index_build:
                calls.append(args)
                return {
                    "ok": True,
                    "job_id": "index-job",
                    "commit_sha": new_base,
                    "tree_sha": "d" * 40,
                    "version": "12.0.0-1",
                    "strategy": "auto",
                    "base_commit_sha": old_base,
                    "status": "running",
                    "step": "snapshot",
                    "deduplicated": False,
                }
            raise AssertionError(function)

        monkeypatch.setattr(mcp_server, "_github_call", fake_github_call)
        raw = await mcp_server.merge_github_pull_request(
            "owner/allowed-repo", 7, expected_head_sha="a" * 40, confirm=True
        )
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["merged"] is True
        assert result["post_merge_index"]["commit_sha"] == new_base
        assert result["post_merge_index"]["base_commit_sha"] == old_base
        assert calls == [(
            mcp_server._service,
            "owner/allowed-repo",
            new_base,
            "auto",
            old_base,
            "interactive",
            f"post-merge-index:owner/allowed-repo:{new_base}",
            False,
        )]

    @pytest.mark.asyncio
    async def test_index_bootstrap_failure_never_reverses_confirmed_merge(self, monkeypatch):
        import json
        import app.mcp_server as mcp_server

        new_base = "c" * 40
        merged = {
            "ok": True,
            "merged": True,
            "repository": "owner/allowed-repo",
            "pull_number": 8,
            "base_head_before": "b" * 40,
            "base_head_after": new_base,
            "merge_commit_sha": new_base,
        }

        async def fake_github_call(function, *args):
            if function is mcp_server.github_utils.merge_github_pull_request:
                return dict(merged)
            if function is mcp_server.mygithub12.request_index_build:
                raise RuntimeError("index backend unavailable")
            raise AssertionError(function)

        monkeypatch.setattr(mcp_server, "_github_call", fake_github_call)
        raw = await mcp_server.merge_github_pull_request(
            "owner/allowed-repo", 8, expected_head_sha="a" * 40, confirm=True
        )
        result = json.loads(raw)
        assert result["ok"] is True
        assert result["merged"] is True
        assert result["post_merge_index"]["ok"] is False
        assert result["post_merge_index"]["status"] == "bootstrap_failed"
        assert result["post_merge_index"]["error_code"] == "RuntimeError"
        assert result["warnings"] == ["POST_MERGE_INDEX_BOOTSTRAP_FAILED"]


    @pytest.mark.asyncio
    async def test_upload_chunk_invalid_base64_returns_stable_error(self):
        import json
        import app.mcp_server as mcp_server

        raw = await mcp_server.append_github_file_upload_chunk(
            "00000000-0000-0000-0000-000000000000",
            40960,
            content_base64="A" * 8621,
            chunk_sha256="f" * 64,
        )
        result = json.loads(raw)
        assert result["ok"] is False
        assert result["error"]["code"] == "UPLOAD_CHUNK_BASE64_INVALID"
        assert result["error"]["details"]["encoded_length"] == 8621

    @pytest.mark.asyncio
    async def test_upload_chunk_utf8_text_bypasses_base64(self, monkeypatch):
        import hashlib
        import json
        import app.mcp_server as mcp_server

        text = "diff --git a/a b/a\n+你好\n"
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        observed = {}

        async def fake_github_call(function, *args):
            assert function is mcp_server.mygithub10.append_upload
            observed["content"] = args[2]
            return {
                "upload_id": args[0],
                "offset": args[1],
                "next_offset": args[1] + len(args[2]),
                "chunk_sha256": args[3],
            }

        monkeypatch.setattr(mcp_server, "_github_call", fake_github_call)
        raw = await mcp_server.append_github_file_upload_chunk(
            "00000000-0000-0000-0000-000000000000",
            0,
            text=text,
            chunk_sha256=digest,
        )
        result = json.loads(raw)
        assert result["next_offset"] == len(encoded)
        assert observed["content"] == encoded
