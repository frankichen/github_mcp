from app import ci_database


def test_log_tail_scans_backwards_past_2000_chunks(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db")); ci_database._local.db = None; ci_database.init_db()
    job = ci_database.create_or_get_job("owner/repo", "main", "c" * 40, "repo-auto-check", 1, 60, True, False, base_sha="b" * 40, changed_files=[])
    for index in range(2105): ci_database.append_log_chunk(job["job_id"], f"line-{index}\n")
    tail = ci_database.get_log_tail(job["job_id"], 100)
    assert tail["lines"][0] == "line-2005" and tail["lines"][-1] == "line-2104"
    assert tail["bytes_scanned"] < 2105 * 20


def test_log_tail_handles_long_utf8_and_secret_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("CI_DB_PATH", str(tmp_path / "ci.db")); ci_database._local.db = None; ci_database.init_db()
    job = ci_database.create_or_get_job("owner/repo", "main", "d" * 40, "repo-auto-check", 1, 60, True, False, base_sha="b" * 40, changed_files=[])
    ci_database.append_log_chunk(job["job_id"], "前" * 500)
    tail = ci_database.get_log_tail(job["job_id"], 1, max_scan_bytes=6000)
    assert tail["lines"] and tail["lines"][0] == "前" * 500
    from app.ci_mcp import _redact_log_line
    assert "[REDACTED]" in _redact_log_line("token=secret-value")
