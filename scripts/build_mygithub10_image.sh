#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "refusing to build from a dirty worktree" >&2
  exit 1
fi

target_sha="$(git rev-parse HEAD)"
if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "HEAD is not a full lowercase Git commit SHA" >&2
  exit 1
fi

git fetch origin
git cat-file -e "${target_sha}^{commit}"

if ! git ls-remote origin | awk -v target="$target_sha" '$1 == target { found = 1 } END { exit !found }'; then
  echo "refusing to build: HEAD is not advertised by origin" >&2
  exit 1
fi

remote_sha="$(gh api "repos/frankichen/github_mcp/commits/${target_sha}" --jq .sha)"
if [[ "$remote_sha" != "$target_sha" ]]; then
  echo "refusing to build: GitHub API did not return the exact HEAD commit" >&2
  exit 1
fi

service_version="$(
  python3 -c '
import ast
from pathlib import Path
tree = ast.parse(Path("services/github-action-service/app/version.py").read_text(encoding="utf-8"))
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "SERVICE_VERSION"
        for target in node.targets
    ):
        print(ast.literal_eval(node.value))
        break
else:
    raise SystemExit("SERVICE_VERSION not found")
'
)"
build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
image_tag="github-action-service:${target_sha}"

docker build \
  --build-arg "SOURCE_REPOSITORY=https://github.com/frankichen/github_mcp" \
  --build-arg "VCS_REF=${target_sha}" \
  --build-arg "SERVICE_VERSION=${service_version}" \
  --build-arg "BUILD_DATE=${build_date}" \
  --tag "$image_tag" \
  --file services/github-action-service/Dockerfile \
  services/github-action-service

docker image inspect "$image_tag" --format \
  'image={{.RepoTags}} id={{.Id}} digest={{json .RepoDigests}} revision={{index .Config.Labels "org.opencontainers.image.revision"}} version={{index .Config.Labels "org.opencontainers.image.version"}} created={{index .Config.Labels "org.opencontainers.image.created"}}'
