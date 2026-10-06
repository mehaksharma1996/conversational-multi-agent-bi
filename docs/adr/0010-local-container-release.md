# ADR 0010: Local container release and restart-persistence waiver

- Status: Accepted
- Date: 2026-09-29
- Issue: [#6](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/6) (Phase 7)

## Context

Phase 7 packages the React/FastAPI product as a health-checked local topology. The
issue's definition of done also asks that the system "survives restart". The API
repository is still process-local (ADR 0006 defers durable metadata), so a restart
loses workspace, dataset, analysis, conversation, and document-index records.
Making them durable means persisting dataframes, profiles, analysis bundles, memory,
and vector indexes behind repository interfaces, migrations, and recovery tests. That
is a persistence project, not a packaging task.

## Decisions

1. **Two purpose-built images plus one optional profile.** `Dockerfile.api` (FastAPI,
   also used by the optional Streamlit compatibility profile) and `Dockerfile.web`
   (static build served by unprivileged nginx with a same-origin proxy). Both are
   multi-stage, digest-pinned, non-root, and carry no secrets.
2. **No worker image.** ADR 0005's criteria for a worker (an operation that exceeds a
   measured request budget, needs durable retry, or causes unsafe contention) have not
   been met or measured. Nothing in Compose assumes a queue.
3. **No telemetry profile yet.** There is no exporter to profile
   ([ADR 0007](0007-observability-privacy.md)); logs are `docker compose logs` and the audit volume.
4. **Hardened by default.** Read-only root filesystems, dropped capabilities,
   `no-new-privileges`, an init process, explicit writable volumes/tmpfs, rotated logs,
   and health checks. The web port is published on loopback only because the API has no
   user authentication yet.
5. **State layout.** `bi-data` (workspace files, short-lived), `bi-audit` (append-only audit
   log), `bi-models` (model cache). Audit is separated so it can be backed up, restored, and
   verified without touching workspace data.
6. **Startup recovery instead of durable metadata.** The API refuses to start if storage is not
   writable, and, in Compose, removes workspace directories that no live process owns
   (`SWEEP_ORPHANED_WORKSPACES`, opt-in, audited as `workspace.expired` with reason `orphan_swept`).
   This guarantees a restart never exposes another tenant's leftover files or leaves user data
   unreachable but retained.
7. **Backup scope is the audit log.** Workspace files cannot be restored to a usable state
   without their metadata, so backing them up would be misleading.

## Waiver: restart persistence

> **Update (2026-10-06):** [ADR 0022](0022-durable-workspace-metadata.md) (issue #12, slice 12a) narrows this
> waiver. With `DURABLE_METADATA=true`, which Compose sets, workspaces, consent state, and uploaded files
> survive a restart. The table below describes the original release and still applies to datasets, analyses,
> conversations, reports, exports, and document indexes until slices 12b and 12c land.

Definition-of-done item "survives restart" is **partially waived**.

| Survives an API/container restart | Does not survive |
|---|---|
| Audit log and its hash chain (`bi-audit`) | Workspaces, uploads, datasets, analyses, conversations, document indexes |
| Model cache (`bi-models`) | Chat history and generated results |
| Configuration and images | Workspace directories (swept at startup) |

- Owner: repository maintainers.
- Follow-up (to be tracked as separate issues, not implied by this release): durable repository
  metadata with migrations and restart recovery; backup/restore of workspace state;
  worker/job durability if measurements justify it; a telemetry profile once an exporter exists.
- Expiry: revisit when the persistence ADR work starts or before any hosted deployment.

## Consequences

- One command starts the whole product locally; a fake-provider-free smoke test (local-only mode) runs in CI.
- Users lose in-progress work on restart; documentation says so plainly.
- Upgrades are image rebuilds; there is no data migration because there is no durable workspace state.
  The audit record format is unchanged and verified by `scripts/verify_audit`.
- Base images must be re-pinned deliberately; the procedure is in the operations guide.

## Invariants

- No secret is baked into an image, a build argument, or the compose file.
- The API is never published directly; the web proxy exposes `/api` and `/health` only.
- Local-only mode and Gemini-enabled mode use the same images.
- Restart recovery never deletes anything outside the API storage root and never follows symlinks.
