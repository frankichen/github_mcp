"""Build and verify reproducible release metadata without touching a Git remote."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(root: str | Path, repository: str, commit_sha: str, tree_sha: str, output: str | Path) -> dict:
    root = Path(root).resolve()
    output = Path(output).resolve()
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == output:
            continue
        relative = path.relative_to(root).as_posix()
        files.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {"repository": repository, "commit_sha": commit_sha, "tree_sha": tree_sha, "files": files}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_name("checksums.sha256").write_text("".join(f"{item['sha256']}  {item['path']}\n" for item in files), encoding="utf-8")
    return manifest


def create_tar_zst(root: str | Path, artifact: str | Path) -> None:
    root = Path(root).resolve()
    artifact = Path(artifact).resolve()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "--zstd", "-cf", str(artifact), "-C", str(root), "."], check=True)


def verify_manifest(root: str | Path, manifest: dict) -> None:
    root = Path(root).resolve()
    for item in manifest.get("files", []):
        path = (root / item["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"artifact path missing or unsafe: {item['path']}")
        if path.stat().st_size != item["size_bytes"] or _sha256(path) != item["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {item['path']}")
