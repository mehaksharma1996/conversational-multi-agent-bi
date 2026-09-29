#!/usr/bin/env sh
# Back up the audit-log volume to <dir>/audit-<UTC timestamp>.tar.gz (default: ./backups).
#
# Only the audit log is backed up. Workspace data (bi-data) is deliberately excluded:
# API metadata is process-local, so workspace files cannot be restored to a usable state
# and they are short-lived by retention policy. See docs/operations/local-containers.md.
#
# Runs as the calling host user (no root, no extra capabilities); audit files are
# world-readable inside the volume.
set -eu

cd "$(dirname "$0")/.."
dest="${1:-backups}"
mkdir -p "$dest"
dest_abs="$(cd "$dest" && pwd)"
archive="audit-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"

docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$dest_abs:/backup" \
  -e "ARCHIVE=$archive" \
  --entrypoint sh api \
  -c 'tar czf "/backup/$ARCHIVE" -C /audit .'

echo "Backup written to $dest/$archive"
