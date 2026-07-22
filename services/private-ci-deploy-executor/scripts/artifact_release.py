"""Build and verify reproducible release metadata without touching a Git remote."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import tarfile
import shutil
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
    entries = [path.relative_to(root).as_posix() for path in sorted(root.rglob("*")) if path.is_file()]
    subprocess.run(["tar", "--zstd", "-cf", str(artifact), "-C", str(root), *entries], check=True)


def verify_manifest(root: str | Path, manifest: dict) -> None:
    root = Path(root).resolve()
    for item in manifest.get("files", []):
        path = (root / item["path"]).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"artifact path missing or unsafe: {item['path']}")
        if path.stat().st_size != item["size_bytes"] or _sha256(path) != item["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {item['path']}")


def _safe_member(name: str) -> bool:
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and not name.startswith("./")


def _extract_zstd_safely(archive: Path, destination: Path, max_files: int = 100000, max_bytes: int = 2 * 1024 * 1024 * 1024) -> set[str]:
    import subprocess
    process = subprocess.Popen(["zstd", "-d", "-q", "-c", str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    names = set(); total = 0
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            for member in tar:
                if len(names) >= max_files or not _safe_member(member.name) or member.name in names:
                    raise ValueError("ARTIFACT_UNSAFE_ARCHIVE_ENTRY")
                if not (member.isdir() or member.isreg()):
                    raise ValueError("ARTIFACT_NON_REGULAR_ENTRY")
                names.add(member.name)
                target = destination / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True); continue
                total += member.size
                if total > max_bytes: raise ValueError("ARTIFACT_SIZE_LIMIT")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None: raise ValueError("ARTIFACT_MEMBER_READ_FAILED")
                with target.open("xb") as output:
                    shutil.copyfileobj(source, output)
        return names
    finally:
        if process.stdout: process.stdout.close()
        process.wait(timeout=30)
        if process.returncode != 0: raise ValueError("ARTIFACT_ZSTD_INVALID")


def build_release_artifact(root: str | Path, output_dir: str | Path, metadata: dict) -> dict:
    """Create the complete release bundle using the pinned tar/zstd toolchain."""
    root = Path(root).resolve(); output_dir = Path(output_dir).resolve()
    if not root.is_dir(): raise ValueError("ARTIFACT_SOURCE_NOT_DIRECTORY")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=output_dir) as staging_name:
        staging = Path(staging_name); payload = staging / "payload"; payload.mkdir()
        for source in sorted(root.rglob("*")):
            if source.is_symlink() or not source.is_file():
                if source.is_symlink(): raise ValueError(f"ARTIFACT_UNSAFE_SYMLINK:{source}")
                continue
            relative = source.relative_to(root); target = payload / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        manifest = build_manifest(payload, metadata["repository"], metadata["commit_sha"], metadata["tree_sha"], staging / "manifest.json")
        manifest.update({k: metadata[k] for k in ("branch", "private_ci_job_id", "source_attestation_id", "profile", "ci_image_digest", "go_version", "node_version", "npm_version") if k in metadata})
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        provenance = {"artifact_format": 1, "repository": metadata["repository"], "branch": metadata.get("branch", "main"), "commit_sha": metadata["commit_sha"], "tree_sha": metadata["tree_sha"], "private_ci_job_id": metadata["private_ci_job_id"], "source_attestation_id": metadata.get("source_attestation_id", ""), "profile": metadata["profile"], "ci_image_digest": metadata["ci_image_digest"], "toolchain": {k: metadata.get(k, "") for k in ("go_version", "node_version", "npm_version")}}
        (staging / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        archive = staging / "release.tar.zst"; create_tar_zst(payload, archive)
        result = {"manifest": manifest, "provenance": provenance, "archive_sha256": _sha256(archive), "archive_size_bytes": archive.stat().st_size}
        destination = output_dir / "release.tar.zst"; destination.write_bytes(archive.read_bytes())
        for name in ("manifest.json", "checksums.sha256", "provenance.json"):
            (output_dir / name).write_bytes((staging / name).read_bytes())
        result["storage_path"] = str(destination); result["archive_path"] = str(destination); result["storage_dir"] = str(output_dir); result["manifest_sha256"] = _sha256(output_dir / "manifest.json"); result["checksums_sha256"] = _sha256(output_dir / "checksums.sha256"); result["provenance_sha256"] = _sha256(output_dir / "provenance.json")
        return result


def verify_release_artifact(artifact_dir: str | Path) -> dict:
    directory = Path(artifact_dir).resolve(); archive = directory / "release.tar.zst"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    checksums = (directory / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    expected = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksums if "  " in line}
    with tempfile.TemporaryDirectory(prefix="verify-") as tmp:
        names = _extract_zstd_safely(archive, Path(tmp))
        listed = {item["path"] for item in manifest.get("files", [])}
        names -= {name for name in names if (Path(tmp) / name).is_dir()}
        if names != listed: raise ValueError("ARTIFACT_ARCHIVE_EXTRA_OR_MISSING_ENTRY")
        verify_manifest(tmp, manifest)
        if any(expected.get(item["path"]) != item["sha256"] for item in manifest.get("files", [])): raise ValueError("ARTIFACT_CHECKSUM_FILE_MISMATCH")
    return {"ok": True, "archive_sha256": _sha256(archive), "files": len(manifest.get("files", []))}
