# Local container release

Phase 7 of [issue #6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6)
packages the product as a health-checked, portable local topology. It adds no cloud, orchestrator,
or hosting-provider assumptions. Operator procedures are in
[local-containers.md](../operations/local-containers.md); decisions and the restart-persistence
waiver are in [ADR 0010](../adr/0010-local-container-release.md).

## Topology

```text
 browser ──► 127.0.0.1:8080 ──► web  (nginx-unprivileged, uid 101, :8080)
                                 │  /            static React build (SPA fallback)
                                 │  /api/*       ─┐ proxied at request time via Docker DNS
                                 │  /health/*    ─┤
                                 ▼               │
                                api (uvicorn, uid 10001, :8000, not published)
                                 │  ├── /data    bi-data    workspace SQLite/Chroma (short-lived)
                                 │  ├── /audit   bi-audit   append-only audit log
                                 │  ├── /models  bi-models  Hugging Face model cache
                                 │  └── /tmp     tmpfs
                                 ▼
                          Gemini (only when a key is set, LOCAL_ONLY_MODE is off,
                                  and the workspace accepted the data-sharing notice)

 optional profile:  streamlit (same api image, uid 10001, 127.0.0.1:8501)
```

## Images

| | `Dockerfile.api` | `Dockerfile.web` |
|---|---|---|
| Stages | builder (venv) → runtime | node build → nginx runtime |
| Base | `python:3.12-slim-bookworm@sha256:…` | `node:24.14.0-bookworm-slim@sha256:…`, `nginx-unprivileged:1.30-alpine@sha256:…` |
| Dependencies | `requirements.txt` constrained by `requirements.lock`; CPU-only PyTorch at the locked version | `npm ci` from `package-lock.json` |
| User | fixed uid/gid 10001, no shell login | uid 101 (base image) |
| Health | `GET /health/ready` (storage and audit writable) | `GET /nginx-health` |
| Secrets | none; runtime environment only | none |
| Startup check | build fails if the API or Streamlit cannot be imported | build fails on type or lint errors in the app |

Measured in CI on Linux: the API image is about 2.25 GB (budget 4.5 GB, which guards against the
multi-gigabyte CUDA PyTorch wheel) and the web image about 59 MB (budget 150 MB).

The API image contains application code only (`apps`, `config`, `packages`, `scripts`, `src`, `app.py`).
Tests, docs, sample data, and `.env` are excluded by `.dockerignore`. The web bundle uses relative URLs,
so no API address is compiled in.

## Runtime hardening

Every service runs with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, an
init process (`init: true`) that forwards signals and reaps children, rotated JSON logs, and a
restart policy. Writable paths are only the three named volumes and `/tmp`. Nothing is bind-mounted from the host.

nginx serves a Content-Security-Policy without `unsafe-eval` (validated against the real application, including
charts, in a real browser in CI), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, and a
restrictive `Permissions-Policy`. It proxies only `/api/` and `/health/`, so the FastAPI docs and OpenAPI
document are not reachable through the web port. The request body limit (60 MB) exceeds the largest permitted upload;
the API enforces the exact limits.

## Lifecycle

1. **Startup.** The API validates configuration (a malformed `APP_ENCRYPTION_KEY` already fails at
   settings load), verifies workspace and audit storage are writable, and refuses to start otherwise.
   With `SWEEP_ORPHANED_WORKSPACES=true` (set by Compose) it deletes workspace directories no live
   process owns, auditing each as `workspace.expired` / `orphan_swept`, then emits `service.started`
   (flags only, no secrets).
2. **Readiness.** `/health/live` reports the process is up; `/health/ready` also checks the storage and audit
   volumes and returns 503 `service_not_ready` when they are unavailable. Compose starts `web` only after `api` is healthy,
   and nginx re-resolves the API address per request, so recreating `api` during an upgrade does not strand the proxy.
3. **Shutdown.** SIGTERM reaches uvicorn (exec-form command under an init process). In-flight requests get 20 seconds,
   open vector indexes are closed, and `service.stopped` is emitted; Compose allows 30 seconds before killing.
4. **Restart.** Process-local workspace records are lost (waiver); leftover directories are swept at the next
   startup; the audit chain continues from its file.

## Verification

| Layer | Check | Where |
|---|---|---|
| Configuration invariants | `tests/test_container_config.py` (non-root, read-only, no secrets, loopback ports, digest pins, CSP, proxy rules, env drift) | pytest, all platforms |
| Runtime behavior | `tests/test_runtime_lifecycle.py` (readiness, fail-closed start, shutdown, orphan sweep) | pytest |
| Operational scripts | `tests/test_ops_scripts.py` (audit verification, model prefetch, smoke script against a live in-process server) | pytest |
| Images and topology | Dockerfile lint, build, size budgets, start with `--wait`, `scripts/compose_smoke.py --compose`, Playwright against the nginx image, backup/restore round trip, Streamlit profile | CI `containers` job (Linux) |

Docker was not available on the machine used to write this phase, so image builds were first exercised by CI rather than
locally; the static and in-process checks above cover everything that does not need a container runtime.

## Limitations

- Workspace state is not durable across restarts (ADR 0010 waiver); exactly one API container is supported.
- No authentication; the web port is loopback-only by default.
- No worker (ADR 0005 criteria unmet), telemetry profile (no exporter), image scanning, or SBOM.
- PDF reports omit chart images because the image excludes a headless Chrome.
- Base-image digests are pinned by hand; automated refresh (for example through Dependabot's Docker ecosystem) is not configured.
- Vector storage remains unencrypted at rest.

## Next phase

Streamlit disposition ([ADR 0009](../adr/0009-streamlit-migration.md)): decide retirement or developer-only retention
against the parity checklist, then close out the remaining definition-of-done items (durable metadata follow-up,
telemetry exporter, image scanning, and independent exercise of the operations documentation).
