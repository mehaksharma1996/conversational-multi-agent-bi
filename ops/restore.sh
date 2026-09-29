#!/usr/bin/env sh
# Restore the audit-log volume from an archive made by ops/backup.sh.
#
#   ops/restore.sh backups/audit-20260929T120000Z.tar.gz
#
# Steps: stop the API (it caches each tenant's chain head in memory), save the current
# audit log to backups/ as a safety copy, replace it with the archive, verify every
# tenant's hash chain, and start the API again. A chain that fails verification aborts
# before the API is restarted.
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: ops/restore.sh <path to audit-*.tar.gz>" >&2
  exit 2
fi

cd "$(dirname "$0")/.."
archive_path="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
archive_dir="$(dirname "$archive_path")"
archive_name="$(basename "$archive_path")"

echo "Stopping the API..."
docker compose stop api

echo "Saving the current audit log before replacing it..."
ops/backup.sh backups

echo "Restoring $archive_name ..."
docker compose run --rm --no-deps \
  -v "$archive_dir:/backup:ro" \
  -e "ARCHIVE=$archive_name" \
  --entrypoint sh api \
  -c 'find /audit -mindepth 1 -delete && tar xzf "/backup/$ARCHIVE" -C /audit'

echo "Verifying audit hash chains..."
docker compose run --rm --no-deps api python -m scripts.verify_audit --require-files

echo "Starting the API..."
docker compose up -d --wait api
echo "Restore complete."
