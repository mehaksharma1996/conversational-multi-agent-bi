<#
.SYNOPSIS
  Restore the audit-log volume from an archive made by ops\backup.ps1.

.DESCRIPTION
  Stops the API (it caches each tenant's chain head in memory), saves the current audit
  log to .\backups as a safety copy, replaces it with the archive, verifies every tenant's
  hash chain, and starts the API again. A chain that fails verification aborts before the
  API is restarted.

.EXAMPLE
  .\ops\restore.ps1 .\backups\audit-20260929T120000Z.tar.gz
#>
param([Parameter(Mandatory = $true)][string]$Archive)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -PathType Leaf $Archive)) {
  Write-Error "Archive not found: $Archive"
  exit 2
}
$archiveFull = (Resolve-Path $Archive).Path
$archiveDir = Split-Path -Parent $archiveFull
$archiveName = Split-Path -Leaf $archiveFull
Set-Location (Split-Path -Parent $PSScriptRoot)

function Invoke-Compose {
  docker compose @args
  if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed (exit $LASTEXITCODE)." }
}

Write-Host "Stopping the API..."
Invoke-Compose stop api

Write-Host "Saving the current audit log before replacing it..."
& (Join-Path $PSScriptRoot "backup.ps1") -Destination "backups"

Write-Host "Restoring $archiveName ..."
# Runs as the image's own non-root user, which owns the audit volume.
Invoke-Compose run --rm --no-deps `
  -v "${archiveDir}:/backup:ro" `
  -e "ARCHIVE=$archiveName" `
  --entrypoint sh api `
  -c 'find /audit -mindepth 1 -delete && tar xzf "/backup/$ARCHIVE" -C /audit'

Write-Host "Verifying audit hash chains..."
Invoke-Compose run --rm --no-deps api python -m scripts.verify_audit --require-files

Write-Host "Starting the API..."
Invoke-Compose up -d --wait api
Write-Host "Restore complete."
