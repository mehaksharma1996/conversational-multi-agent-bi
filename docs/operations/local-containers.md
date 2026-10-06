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
identity. The API can verify OIDC Bearer JWTs when `API_AUTH_MODE=oidc` and the issuer, audience,
JWKS URL, and asymmetric algorithms are configured, but the bundled React client does not yet
perform browser sign-in or attach tokens. Logout, CSRF, and the final CORS policy also remain
open under issue #9. Browser sessions (cookie, CSRF, logout) are implemented per ADR 0013 but cannot
yet be created because login/callback is pending. API authorization is capability-based: local mode grants every capability to its
single loopback-only user, while OIDC mode grants only what the verified `OIDC_ROLES_CLAIM` roles
(`viewer`, `analyst`, `workspace_admin`) map to. Compose therefore publishes the web port on `127.0.0.1` only; do not change the
mapping to `0.0.0.0` or put the service on a shared network yet.

The OIDC foundation is intended for direct API clients during this phase. Required settings are
listed in `.env.example`. The API validates `iss`, `aud`, `sub`, `exp`, optional time claims, and the
configured asymmetric signature algorithm against a bounded in-memory JWKS cache. Partial OIDC
configuration fails startup rather than activating local mode.

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
| `bi-data` | `/data` | Per-workspace SQLite and Chroma files | Files do, but records do not (see below) | No |
| `bi-audit` | `/audit` | Append-only audit log (`<tenant>.jsonl`) | Yes | **Yes** |
| `bi-models` | `/models` | Hugging Face model cache | Yes | Optional |

**What a restart does.** API workspace metadata is process-local. After `docker compose restart api`
(or any crash), in-progress workspaces are gone: users start a new workspace and re-upload. On
startup the API deletes workspace directories no live process owns, so no unreachable user data
lingers. The audit log is unaffected. This is a documented waiver, not a bug.

Other knobs (limits, retention, retrieval distance) are environment variables listed in
`.env.example`; add any of them under `environment:` in a `compose.override.yaml`.

## Stop, restart, reset

```powershell
docker compose stop                # stop, keep everything
docker compose restart api         # restart the API (workspaces are lost, audit is kept)
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

- Workspace metadata is process-local (waiver above); run exactly one API container.
- No user authentication; loopback only.
- No worker, telemetry profile, image scanning, or SBOM yet (see ADR 0010 follow-ups).
- Compose and the smoke test were validated in CI on Linux; other Docker hosts (Docker Desktop on
  Windows/macOS) use the same images but are not part of CI.
