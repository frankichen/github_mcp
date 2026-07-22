#!/usr/bin/env python3
"""Test MCP commit_github_files with various payload sizes."""
import json
import sys
import subprocess
import ssl
import urllib.request

def main() -> None:
    api_key = sys.argv[1] if len(sys.argv) > 1 else ""
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 2000

    payload = subprocess.run(
        ["python3", "/opt/github-action-service/scripts/generate_payload.py", str(size)],
        capture_output=True, text=True
    ).stdout

    files_json = json.dumps([{"path": f"PAYLOAD_{size}B_TEST.md", "operation": "upsert", "content": payload}])

    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "commit_github_files",
            "arguments": {
                "repository": "frankichen/ai_war",
                "branch": "ai/mygithub2-service-doc",
                "commit_message": f"test: {size}b payload via MCP",
                "files_json": files_json,
            }
        }
    }

    req = urllib.request.Request(
        "https://github.555044.xyz/mcp",
        data=json.dumps(rpc_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context()) as resp:
        body = resp.read().decode("utf-8")
        for line in body.split("\n"):
            if line.startswith("data:"):
                result = json.loads(line[5:].strip())
                if "result" in result:
                    content = result["result"].get("content", [{}])[0].get("text", "{}")
                    parsed = json.loads(content)
                    print(json.dumps({"commit_sha": parsed.get("commit_sha", ""), "files": parsed.get("changed_files", [])}, indent=2))
                elif "error" in result:
                    print(f"RPC error: {result['error']}")


if __name__ == "__main__":
    main()
