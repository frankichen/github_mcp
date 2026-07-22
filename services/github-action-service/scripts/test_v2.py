#!/usr/bin/env python3
"""Test commit_github_files_v2 (both formats)"""
import json, sys, ssl, urllib.request

API_KEY = sys.argv[1]

for fmt in ["string", "list"]:
    content = f"# V2 test ({fmt})\nLine 2\nLine 3\n"
    
    # prepare the files param
    files_data = [{"path": f"V2_TEST_{fmt}.md", "operation": "upsert", "content": content}]
    if fmt == "string":
        files_arg = json.dumps(files_data)
    else:
        files_arg = files_data

    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "commit_github_files_v2",
            "arguments": {
                "repository": "frankichen/ai_war",
                "branch": "ai/mygithub2-service-doc",
                "commit_message": f"test: v2 {fmt} format",
                "files": files_arg,
            }
        }
    }

    req = urllib.request.Request(
        "https://github.555044.xyz/mcp",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "Accept": "application/json, text/event-stream",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx) as resp:
        body_text = resp.read().decode()
        for line in body_text.split("\n"):
            if line.startswith("data:"):
                result = json.loads(line[5:].strip())
                if "result" in result:
                    text = result["result"]["content"][0]["text"]
                    parsed = json.loads(text)
                    if "error" in parsed:
                        print(f"V2 {fmt}: FAIL - {parsed['error']}: {parsed.get('message','')}")
                    else:
                        print(f"V2 {fmt}: OK sha={parsed.get('commit_sha','')}")
