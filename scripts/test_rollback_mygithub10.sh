#!/usr/bin/env bash
set -euo pipefail

root="$(mktemp -d)"
trap 'rm -rf "$root"' EXIT
bin="$root/bin"; mkdir -p "$bin"
cat > "$bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "image" && "$2" == "inspect" ]]; then exit 0; fi
if [[ "$1" == "inspect" ]]; then
  format="${3:-}"
  if [[ "$format" == *".Image"* ]]; then echo "sha256:old"; else echo "healthy"; fi
  exit 0
fi
if [[ "$1" == "compose" ]]; then exit 0; fi
exit 0
MOCK
chmod +x "$bin/docker"
snapshot="$root/snapshot"
mkdir -m 700 "$snapshot"
cat > "$snapshot/snapshot.meta" <<EOF
snapshot_id=test
created_at=2026-01-01T00:00:00Z
container_name=github-action-service
compose_file=$root/docker-compose.yml
previous_image_id=sha256:old
EOF
touch "$root/docker-compose.yml"

PATH="$bin:$PATH" MYGITHUB10_ROLLBACK_BACKUP_ROOT="$root/backups" \
  scripts/rollback_mygithub10.sh --snapshot "$snapshot" >/dev/null
test ! -d "$root/backups"
PATH="$bin:$PATH" MYGITHUB10_ROLLBACK_BACKUP_ROOT="$root/backups" \
  scripts/rollback_mygithub10.sh --snapshot "$snapshot" --confirm >/dev/null
override="$root/backups"/*/compose.override.yml
grep -q 'image: sha256:old' $override
echo "rollback mock tests passed"
