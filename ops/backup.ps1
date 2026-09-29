<#
.SYNOPSIS
  Back up the audit-log volume to <Destination>\audit-<UTC timestamp>.tar.gz (default .\backups).

.DESCRIPTION
  Only the audit log is backed up. Workspace data (bi-data) is deliberately excluded:
  API metadata is process-local, so workspace files cannot be restored to a usable state
  and they are short-lived by retention policy. See docs/operations/local-containers.md.
#>
param([string]$Destination = "backups")

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$destinationFull = (Resolve-Path $Destination).Path
$archive = "audit-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')).tar.gz"

# Docker Desktop bind mounts on Windows do not enforce Unix ownership, so root can write
# the archive; on Linux/macOS use ops/backup.sh, which runs as your own user instead.
docker compose run --rm --no-deps `
  --user 0 `
  -v "${destinationFull}:/backup" `
  -e "ARCHIVE=$archive" `
  --entrypoint sh api `
  -c 'tar czf "/backup/$ARCHIVE" -C /audit .'
if ($LASTEXITCODE -ne 0) { throw "Backup failed (exit $LASTEXITCODE)." }

Write-Host "Backup written to $Destination\$archive"
