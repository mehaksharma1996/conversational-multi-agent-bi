# Running the workbench with Docker Compose

Everything runs locally; nothing here assumes a hosting provider. Decisions and the
restart-persistence waiver are recorded in [ADR 0010](../adr/0010-local-container-release.md).

## Requirements

- Docker Engine or Docker Desktop with Compose v2.17+ (`docker compose version`).
- About 4 GB of memory for Docker and roughly 2.5 GB of disk for the images (measured in CI: the API image
  is about 2.25 GB because of scientific Python and CPU-only PyTorch; the web image is about 60 MB), plus
  about 100 MB for the embedding model. CI fails if the API image exceeds 4.5 GB.
- Network access for the first build, and for the first PDF upload unless you prefetch the model
  (see "Offline use").

## Start

```powershell
docker compose up -d --build --wait
```

Open <http://127.0.0.1:8080>. `--wait` returns when both services are healthy.

Choose the mode without rebuilding any image:

| Mode | How |
|---|---|
| Deterministic only (no data ever leaves your machine) | `$env:LOCAL_ONLY_MODE = "true"; docker compose up -d --wait` |
| Gemini-enabled | Put `GEMINI_API_KEY=...` in a local `.env` (never committed), then `docker compose up -d --wait`. Users still accept the data-sharing notice in the UI. |

Compose reads `.env` from the repository root for these values. They are passed as runtime
environment variables, never baked into images or build arguments. See
[`.env.example`](../../.env.example) for every setting and
[responsible-ai.md](../governance/responsible-ai.md#data-sent-to-gemini) for what Gemini mode sends.

### Exposure warning

Compose defaults to `API_AUTH_MODE=local`, where every request runs as one fixed development
identity with every capability. With `API_AUTH_MODE=oidc` the API verifies OIDC Bearer JWTs for API
clients and, when the `OIDC_*` browser-login settings and `WEB_ORIGIN` are configured, signs browsers
in with an authorization-code + PKCE flow and an HttpOnly session cookie (ADR 0013). The React client
asks `GET /api/v1/auth/config` how to authenticate, shows a sign-in screen when needed, sends the
per-session CSRF token on state-changing requests, and returns to sign-in when a session expires.
Capabilities come only from the verified `OIDC_ROLES_CLAIM` roles (`viewer`, `analyst`,
`workspace_admin`). API and nginx access logs record paths without query strings so authorization
codes never reach logs.

Compose still publishes the web port on `127.0.0.1` only. Do not change the mapping to `0.0.0.0` or
put the service on a shared network until the remaining issue #9 work (authentication audit events
and a Docker-validated stack) is complete.

Change the port with `WEB_PORT` (default `8080`).

## Verify

```powershell
python scripts/compose_smoke.py --compose --expect-local-only
```

(Use `LOCAL_ONLY_MODE=true` when starting.) The smoke test drives the same HTTP surface a browser
uses: readiness, workspace, CSV upload, profile, schema confirmation, analysis, chat (deterministic
session-memory route), Markdown and PDF reports, and deletion. With `--compose` it also checks that
the API and web run as non-root, the API filesystem is read-only, security headers are present, and
that after an API restart the service recovers, orphaned workspace directories are swept, and the
audit hash chain continues. PDF indexing is not part of the smoke test because it needs the
embedding model download; it is covered by the API tests and evaluations.

## Configuration and data

| Volume | Mounted at | Holds | Persists across restart | Back up? |
|---|---|---|---|---|
| `bi-data` | `/data` | Workspace metadata database (`api/.metadata/`), uploaded files, per-workspace SQLite and Chroma files | Everything a user creates in a workspace does (see below); only a pending SQL approval and jobs do not | **Yes** ([workspace backup](#backup-and-restore-workspace-state)) |
| `bi-audit` | `/audit` | Append-only audit log (`<tenant>.jsonl`) | Yes | **Yes** |
| `bi-models` | `/models` | Hugging Face model cache | Yes | Optional |

**What a restart does.** Compose sets `DURABLE_METADATA=true` ([ADR 0022](../adr/0022-durable-workspace-metadata.md)).
After `docker compose restart api` (or a crash) the API reopens a versioned SQLite metadata database,
migrates it forward, and restores workspaces, their consent state, hashed idempotency keys, and uploaded
files (each verified by SHA-256 when read), then rebuilds datasets, analyses, and reports from those inputs
the first time a workspace is used (the same deterministic processing, so results match; the first request
after a restart pays that cost once). Expired workspaces are removed through the audited path, rows whose
files are missing are dropped (with anything built on them), and directories no row owns are swept.
Conversations, messages, and exports come back from the workspace's own `content.db` (encrypted when
`APP_ENCRYPTION_KEY` is set), and the document index is reopened, not re-embedded, after its chunk count is
verified. **Not recovered:** a SQL approval that was pending at the time (it is marked rejected and nothing
runs) and jobs. Startup refuses to run on a database written by a newer build, and quarantines an
unreadable database (`metadata.db.corrupt-<UTC stamp>`) without sweeping the directories it can no longer
account for. Local development without Compose keeps the old process-local behaviour unless
`DURABLE_METADATA=true` is set.

Other knobs (limits, retention, retrieval distance) are environment variables listed in
`.env.example`; add any of them under `environment:` in a `compose.override.yaml`.

## Stop, restart, reset

```powershell
docker compose stop                # stop, keep everything
docker compose restart api         # restart the API (workspace state recovers; see above)
docker compose down                # remove containers, keep volumes
docker compose down -v             # remove containers AND all volumes: a full reset
```

Reset only workspace files, keeping audit and the model cache:

```powershell
docker compose stop api
docker compose run --rm --no-deps --entrypoint sh api -c "rm -rf /data/api"
docker compose up -d --wait api
```

Deleting a workspace in the UI removes its files immediately and records `workspace.deleted` in the
audit log; audit records are content-free and intentionally survive deletion.

## Logs and health

```powershell
docker compose ps                          # health status
docker compose logs -f api                 # JSON telemetry lines + server logs
docker compose logs --no-color web
docker compose exec api python -m scripts.verify_audit          # audit hash chains
docker compose exec api sh -c "cat /audit/*.jsonl"              # raw audit records
```

Container logs rotate at 10 MB x 3 files. To investigate a user-reported error, follow the
[incident runbook](incident-debugging.md) using the request ID shown in the UI. Readiness
(`/health/ready`) fails with 503 if the data or audit volume stops accepting writes.

## Backup and restore (audit log)

```powershell
.\ops\backup.ps1                              # writes .\backups\audit-<UTC stamp>.tar.gz
.\ops\restore.ps1 .\backups\audit-<stamp>.tar.gz
```

On Linux or macOS use `sh ops/backup.sh` and `sh ops/restore.sh <archive>`.

Restore stops the API, saves the current audit log to `backups/` as a safety copy, replaces the
volume contents, verifies every tenant's hash chain, and only then restarts the API. If
verification fails, the API stays stopped so you can investigate; nothing is deleted from your
`backups/` directory. Backups contain no user content (audit records are content-free) but do
contain opaque tenant identifiers, so treat them as internal.

## Audit rotation and retention

Compose sets `AUDIT_MAX_SEGMENT_BYTES` to 64 MiB: when a tenant's audit file reaches that size it is sealed
as `<tenant>.<NNNNNN>.jsonl` and the same hash chain continues in a new file, so `scripts.verify_audit` keeps
working across segments and the existing backup and restore (`ops/backup.*`, `ops/restore.*`) include every
segment. To archive old history, stop (or idle) the API and run:

```powershell
docker compose exec api python -m scripts.audit_maintenance status
docker compose exec api python -m scripts.audit_maintenance prune --archive-dir /audit/archive --keep-segments 3
docker compose exec api python -m scripts.verify_audit --require-files
```

`prune` refuses to run on a broken chain, moves (never deletes) the older sealed segments into the archive
directory, and records an anchor so the retained chain still verifies. Copy the archive off the host: the
anchor proves where the retained chain starts, not what came before. This procedure is exercised by
`tests/test_audit_rotation.py`; the `docker compose exec` invocations themselves are not run in CI.

## Backup and restore (workspace state)

`scripts/workspace_backup.py` writes one `.tar.gz` holding a consistent copy of the metadata database,
every file of every workspace it knows, and a manifest of SHA-256 digests. Stop the API first for a clean
point-in-time image of workspace files (the database copy is consistent either way):

```powershell
docker compose stop api
mkdir backups -Force
docker compose run --rm --no-deps -v "${PWD}/backups:/backup" --entrypoint python api `
  -m scripts.workspace_backup backup --storage-root /data/api --output /backup/workspaces.tar.gz
docker compose run --rm --no-deps -v "${PWD}/backups:/backup" --entrypoint python api `
  -m scripts.workspace_backup verify /backup/workspaces.tar.gz
docker compose up -d --wait api
```

`verify` and `restore` check every digest, reject unsafe archive paths, run SQLite integrity checks on the
metadata and on any Chroma index database, and confirm ownership: each file lives under the tenant its row
names, each upload matches its recorded digest, and no file exists that metadata does not own. To restore:

```powershell
docker compose stop api
docker compose run --rm --no-deps -v "${PWD}/backups:/backup" --entrypoint python api `
  -m scripts.workspace_backup restore /backup/workspaces.tar.gz --storage-root /data/api --replace
docker compose up -d --wait api
```

Restore refuses while the database is in use, verifies before changing anything, and never deletes the
previous state: with `--replace` it is moved to `/data/api.pre-restore-<UTC stamp>` for you to remove once
satisfied. Backups contain uploaded user data; protect them like the data itself.

The backup, verification, tamper, ownership, and restore logic is covered by `tests/test_workspace_backup.py`.
The `docker compose run` invocations above are **not** exercised by CI. The containers run as uid 10001, so on
Linux the host `backups/` directory must be writable by that user (for example `chmod 777 backups`, then remove
the archive's world access afterwards); Docker Desktop on Windows and macOS needs no change.

## Upgrade

```powershell
git pull
docker compose build --pull
docker compose up -d --wait
python scripts/compose_smoke.py --compose --expect-local-only   # optional confidence check
```

- No data migration is required: workspace state is not durable, and the audit record format is
  unchanged (`verify_audit` confirms the chain).
- Restarting the API ends active workspaces; warn users first.
- **Re-pinning base images.** Dockerfiles pin base images by digest. To move to a newer
  base, resolve the tag's new digest (for example
  `docker buildx imagetools inspect python:3.12-slim-bookworm`), update the `ARG ..._IMAGE=` line in
  `Dockerfile.api` / `Dockerfile.web`, rebuild, and run the smoke test. `tests/test_container_config.py`
  fails if a base image is not digest-pinned.
- Python dependencies come from `requirements.txt` constrained by `requirements.lock`;
  frontend dependencies from `npm ci`. Regenerate the lock file deliberately, then rebuild.

## Offline use

Prefetch the embedding model once while online, then forbid downloads:

```powershell
docker compose run --rm api python -m scripts.prefetch_model
$env:HF_HUB_OFFLINE = "1"; docker compose up -d --wait
```

`LOCAL_ONLY_MODE=true` already disables every Gemini-backed path. Without Gemini, PDF indexing
and document retrieval still work offline once the model is cached, but document *answers* need Gemini.

## Model providers

Providers are selected by environment only: `LLM_PROVIDERS` is an ordered fallback chain (`gemini`, `anthropic`,
`ollama`); the default is `gemini`. Hosted keys (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) are passed from your shell or `.env`,
never baked into images. `LOCAL_ONLY_MODE=true` withholds every hosted key. For a local model run Ollama on the host or in a
sibling container and set `OLLAMA_MODEL`; the endpoint counts as local only when it is loopback or its host name is listed in
`OLLAMA_TRUSTED_HOSTS` (for example `ollama` for a Compose service name), which is an assertion you make. A non-local Ollama URL is
treated like a hosted provider. `LLM_TIER_<PURPOSE>` (`fast` or `strong`) with `*_MODEL_FAST` lets cheap models classify while stronger ones write
SQL and answers. See [ADR 0017](../adr/0017-model-providers-and-fallback.md).

## Optional Streamlit compatibility UI

```powershell
docker compose --profile streamlit up -d --wait streamlit   # http://127.0.0.1:8501
```

It uses the same API image and keeps its own per-session data below `/data/sessions`. Streamlit does not
emit the API's audit events. Its disposition is governed by [ADR 0009](../adr/0009-streamlit-migration.md).

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `port is already allocated` | Set `WEB_PORT` (or `STREAMLIT_PORT`) to a free port. |
| `docker compose up --wait` times out on `api` | `docker compose logs api`. The first start imports large libraries and can take a minute on a slow disk. A `not writable` startup error means the volume's permissions are wrong: `docker compose down -v` recreates it with the correct owner. |
| UI shows "service not ready" (503) | The API cannot write to `/data` or `/audit`. Check disk space and volume health. |
| PDF upload fails with an embedding/model error | The model could not download. Check network or proxy settings, or prefetch it (see "Offline use"). |
| PDF report has no chart images | Chart export needs a headless Chrome that the image deliberately does not include; reports still contain all tables and text. |
| Everything is gone after a restart | Expected: workspace state is not durable (ADR 0010). Audit is preserved. |
| Permission errors on Linux bind mounts | The compose file uses named volumes on purpose; if you switch to bind mounts, `chown 10001:10001` the host directories. |
| Very slow analysis or out-of-memory kills | Give Docker at least 4 GB; reduce `MAX_TABULAR_ROWS` and upload sizes. |

## Known limitations

- Workspace state is durable (ADR 0022; the ADR 0010 waiver is closed except for pending approvals and
  jobs). Run exactly one API container.
- Conversation content is stored in each workspace's `content.db`; it is plain SQLite unless
  `APP_ENCRYPTION_KEY` is set, and changing that key makes earlier encrypted content unreadable.
- No user authentication; loopback only.
- No worker, telemetry profile, image scanning, or SBOM yet (see ADR 0010 follow-ups).
- Compose and the smoke test were validated in CI on Linux; other Docker hosts (Docker Desktop on
  Windows/macOS) use the same images but are not part of CI.
