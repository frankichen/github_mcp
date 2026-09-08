import json
from pathlib import Path

import pytest

from app import development_failure_pack as failure_pack
from app import development_failure_pack_store as failure_pack_store
from app import mcp_response, mygithub12


def _job(**overrides):
    job = {
        "job_id": "job-failure-pack-010",
        "repository": "owner/repo",
        "branch": "feature/failure-pack",
        "commit_sha": "a" * 40,
        "base_sha": "b" * 40,
        "profile": "repo-auto-check",
        "status": "failed",
        "exit_code": 1,
        "error_code": "CI_STEP_FAILED",
        "error_message": "test command failed",
        "changed_files": [
            {"path": "src/math.py", "operation": "modified"},
            {"path": "tests/test_math.py", "operation": "modified"},
        ],
        "changed_files_total": 2,
        "summary": {
            "steps": [
                {
                    "step_name": "pytest",
                    "status": "failed",
                    "exit_code": 1,
                    "command": "pytest --token=top-secret tests/test_math.py",
                }
            ],
        },
    }
    job.update(overrides)
    return job


def test_redact_text_large_non_secret_bypasses_sensitive_assignment_regex(monkeypatch):
    class ExplodingPattern:
        def sub(self, *_args, **_kwargs):
            raise AssertionError("expensive assignment regex should be bypassed")

    monkeypatch.setattr(failure_pack, "_SENSITIVE_ASSIGNMENT_RE", ExplodingPattern())
    text = "secret appears in documentation; " + ("ordinary=value;" * 4096)

    assert failure_pack.redact_text(text) == text


def test_redact_text_large_secret_near_tail_still_redacts():
    secret = "repair-secret-value-12345"
    text = ("ordinary=value;" * 4096) + f" client_secret={secret}"

    redacted = failure_pack.redact_text(text)

    assert secret not in redacted
    assert "client_secret=[REDACTED]" in redacted


def test_parse_failure_location_large_no_location_uses_non_overlapping_tokens(monkeypatch):
    original = failure_pack._parse_location_token
    inspected_lengths = []

    def counting_parse(token):
        inspected_lengths.append(len(token))
        return original(token)

    monkeypatch.setattr(failure_pack, "_parse_location_token", counting_parse)
    text = ("plain-diagnostic-segment:" * 4096) + "not-a-location"

    location = failure_pack.parse_failure_location(text)

    assert location["status"] == "unavailable"
    assert sum(inspected_lengths) <= len(text)
    assert len(inspected_lengths) == 1


@pytest.mark.parametrize(
    ("location_text", "expected_file", "expected_line", "expected_column"),
    [
        ("/workspace/src/tail.py:321:9", "/workspace/src/tail.py", 321, 9),
        (r"C:\workspace\src\tail.py:654:2", r"C:\workspace\src\tail.py", 654, 2),
    ],
)
def test_parse_failure_location_large_text_finds_location_near_tail(
    location_text, expected_file, expected_line, expected_column
):
    text = ("noise " * 8192) + location_text

    location = failure_pack.parse_failure_location(text)

    assert location == {
        "status": "complete",
        "file": expected_file,
        "line": expected_line,
        "column": expected_column,
    }


def test_parse_failure_location_rejects_invalid_location_candidates():
    text = "https://example.test:443/path file.py:not-a-line C:\\temp\\bad.py:line"

    assert failure_pack.parse_failure_location(text)["status"] == "unavailable"


def test_failure_parser_handles_pytest_go_and_node_locations():
    log = "\n".join(
        [
            "E   AssertionError: expected 2, got 3",
            "tests/test_math.py:17:3: AssertionError",
            "FAILED tests/test_math.py::TestMath::test_add[param] - AssertionError",
            "--- FAIL: TestServer/handles_error (0.01s)",
            "    server_test.go:42:7: got bad response",
            "● api client › retries a request",
            "    at /workspace/src/api.test.js:12:4",
        ]
    )

    parsed = failure_pack.parse_failed_tests(log)

    assert {item["framework"] for item in parsed} == {"pytest", "go", "node"}
    pytest_test = next(item for item in parsed if item["framework"] == "pytest")
    assert pytest_test["name"] == "TestMath::test_add[param]"
    assert pytest_test["file"] == "tests/test_math.py"
    assert pytest_test["line"] == 17
    assert pytest_test["column"] == 3
    assert pytest_test["location_status"] == "complete"
    go_test = next(item for item in parsed if item["framework"] == "go")
    assert go_test["name"] == "TestServer/handles_error"
    assert go_test["file"] == "server_test.go"
    assert go_test["line"] == 42
    assert go_test["column"] == 7
    node_test = next(item for item in parsed if item["framework"] == "node")
    assert node_test["file"] == "/workspace/src/api.test.js"
    assert node_test["line"] == 12
    assert node_test["column"] == 4


@pytest.mark.parametrize(
    ("job", "log", "expected"),
    [
        ({"status": "failed", "exit_code": 1}, "SyntaxError: invalid syntax", "code"),
        ({"status": "failed", "exit_code": 1}, "FAILED tests/a.py::test_x", "test"),
        ({"status": "failed", "exit_code": 1}, "npm ERR! ERESOLVE unable to resolve dependency tree", "dependency"),
        ({"status": "failed", "exit_code": 1}, "permission denied: /workspace/file", "permission"),
        ({"status": "failed", "exit_code": 1}, "connection reset by peer", "network"),
        ({"status": "failed", "exit_code": 1}, "runner unavailable", "runner"),
        ({"status": "failed", "exit_code": 1}, "database is locked", "infrastructure"),
        ({"status": "timed_out", "exit_code": 124}, "command exceeded deadline", "timeout"),
        ({"status": "cancelled", "exit_code": 130}, "cancelled by worker", "cancelled"),
        ({"status": "failed", "exit_code": 1}, "exit code 1", "unknown"),
    ],
)
def test_failure_classification_codes(job, log, expected):
    assert failure_pack.classify_failure(job, log) == expected


def test_failure_pack_is_durable_deduplicated_and_rematerializable(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    monkeypatch.setattr(failure_pack, "get_steps", lambda job_id: [])
    monkeypatch.setattr(failure_pack, "get_log_tail", lambda *args, **kwargs: {"lines": []})
    log = "FAILED tests/test_math.py::test_add - AssertionError\nE   token=ghp_not-for-output\n"
    affected = {
        "complete": True,
        "changed_paths": ["src/math.py"],
        "selected_tests": ["tests/test_math.py::test_add"],
    }

    first = failure_pack.build_failure_pack(
        _job(command="pytest --token=top-secret tests/test_math.py"),
        affected=affected,
        log_tail=log,
    )
    second = failure_pack.build_failure_pack(
        _job(command="pytest --token=top-secret tests/test_math.py"),
        affected=affected,
        log_tail=log,
    )

    assert first["failure_pack_id"] == second["failure_pack_id"]
    assert len(first["failure_pack_id"]) == 64
    assert first["job_identity"]["job_id"] == "job-failure-pack-010"
    assert first["failed_step"]["step_name"] == "pytest"
    assert first["failed_step"]["exit_code"] == 1
    assert first["classification"] == "test"
    assert first["error_fingerprint"]
    assert first["affected_tests"] == ["tests/test_math.py::test_add"]
    assert "top-secret" not in json.dumps(first)
    assert "ghp_not-for-output" not in json.dumps(first)
    assert "[REDACTED]" in first["redacted_command"]

    failure_pack_store.init_failure_pack_db()
    with mygithub12._db() as db:
        count = db.execute("SELECT COUNT(*) FROM development_failure_packs").fetchone()[0]
    assert count == 1

    durable = failure_pack.read_failure_pack(first["failure_pack_id"])
    assert durable is not None
    assert durable["failure_pack_id"] == first["failure_pack_id"]
    assert durable["log_excerpt"]["content"]

    resource_uri = first["resource_uri"]
    assert resource_uri
    assert json.loads(mcp_response.read_response_resource_text(resource_uri))["failure_pack_id"] == first["failure_pack_id"]
    resource_meta = Path(tmp_path / "resources" / f"{resource_uri.rsplit('/', 1)[-1]}.meta.json")
    expired = json.loads(resource_meta.read_text(encoding="utf-8"))
    expired["expires_at"] = 0
    resource_meta.write_text(json.dumps(expired), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mcp_response.read_response_resource_text(resource_uri)

    monkeypatch.setattr(
        failure_pack,
        "get_log_tail",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CI log read is not allowed during rematerialization")),
    )
    rematerialized = failure_pack.materialize_failure_pack(first["failure_pack_id"])
    assert rematerialized["failure_pack_id"] == first["failure_pack_id"]
    assert rematerialized["resource_uri"] != resource_uri
    assert json.loads(mcp_response.read_response_resource_text(rematerialized["resource_uri"])) == durable
    assert rematerialized["rematerialize"]["rerun_ci"] is False


def test_failure_pack_bounds_log_and_marks_missing_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("MYGITHUB12_DB_PATH", str(tmp_path / "controller.db"))
    monkeypatch.setenv("MCP_RESPONSE_RESOURCE_DIR", str(tmp_path / "resources"))
    monkeypatch.setattr(failure_pack, "get_steps", lambda job_id: [])
    job = _job(
        job_id="job-bounded",
        current_step=None,
        summary={"steps": []},
        error_code="",
        error_message="",
    )
    oversized_log = "x" * (failure_pack.MAX_LOG_EXCERPT_BYTES + 512)
    result = failure_pack.build_failure_pack(job, log_tail=oversized_log)
    durable = failure_pack.read_failure_pack(result["failure_pack_id"])

    assert durable is not None
    assert len(durable["log_excerpt"]["content"].encode("utf-8")) <= failure_pack.MAX_LOG_EXCERPT_BYTES
    assert durable["log_excerpt"]["status"] == "partial"
    assert durable["failed_step_evidence"]["status"] == "unavailable"
    assert durable["failed_tests_evidence"]["status"] == "unavailable"
    assert durable["command_evidence"]["status"] == "unavailable"
    assert durable["primary_errors_evidence"]["status"] == "unavailable"
