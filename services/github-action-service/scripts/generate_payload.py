#!/usr/bin/env python3
"""Generate deterministic markdown payload of a specific size for truncation testing."""

import sys
import json

TEMPLATE_LINE = "# Line {idx:05d} - payload fill pattern abcdefghijklmnopqrstuvwxyz0123456789 END\n"


def generate_payload(target_bytes: int) -> str:
    line = TEMPLATE_LINE.format(idx=99999)
    line_bytes = len(line.encode("utf-8"))
    repeat = (target_bytes // line_bytes) + 2
    full = "".join(TEMPLATE_LINE.format(idx=i) for i in range(repeat))
    raw = full.encode("utf-8")[:target_bytes]
    payload = raw.decode("utf-8", errors="replace").rstrip("\ufffd")
    while len(payload.encode("utf-8")) < target_bytes:
        payload += "X"
        payload = payload.encode("utf-8")[:target_bytes].decode("utf-8", errors="replace").rstrip("\ufffd")
    actual = len(payload.encode("utf-8"))
    assert actual == target_bytes, f"Expected {target_bytes} bytes, got {actual}"
    return payload


def main():
    if len(sys.argv) < 2:
        print("Usage: generate_payload.py <bytes> [--json]")
        print("  <bytes>: target payload size in bytes")
        print("  --json:  wrap in a test commit request body")
        sys.exit(1)

    target = int(sys.argv[1])
    content = generate_payload(target)

    if "--json" in sys.argv:
        body = {
            "repository": "frankichen/ai_war",
            "branch": "ai/mygithub2-service-doc",
            "commit_message": f"test: {target}b payload diagnostic",
            "files": [
                {
                    "path": f"test_{target}b.md",
                    "operation": "upsert",
                    "content": content,
                }
            ],
        }
        print(json.dumps(body))
    else:
        sys.stdout.write(content)


if __name__ == "__main__":
    main()
