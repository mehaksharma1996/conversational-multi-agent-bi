# Independent validation plan (issue #11)

Purpose: have someone other than the implementer follow the documentation from a clean machine and record
what happened, so defects in the *documents* (not just the code) are found. The tester should use only the
repository and the linked guides, and should say when they had to guess.

## Tester and environment record

| Field | Value |
|---|---|
| Tester (not the implementer) | |
| Date and commit SHA (`git rev-parse HEAD`) | |
| OS and version | |
| Docker and Compose versions (`docker --version`, `docker compose version`) | |
| Python and Node versions (only for the developer checks) | |
| Model provider used, or "none (local-only)" | |
| Network restrictions, if any | |

## How to record each step

For every step write **Result** (`PASS`, `FAIL`, `BLOCKED`, `UNCLEAR`), **Evidence** (a command output
excerpt, a screenshot name, or a file path), and **Notes** (anything you had to guess, any wording that
misled you). `UNCLEAR` means the document did not say enough to proceed without guessing: that is a
documentation defect even if you eventually succeeded.

## Steps

Prerequisites for all: Docker with Compose, a clean checkout, and no existing `bi-*` volumes.

### A. Start and verify ([local containers](local-containers.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| A1 | `docker compose up -d --build --wait` | Both services healthy; only `127.0.0.1:8080` is published | | | |
| A2 | `python scripts/compose_smoke.py --compose --expect-local-only` (needs Python) | Every check prints `ok`; ends with success | | | |
| A3 | Open `http://127.0.0.1:8080` | Workspace ready, retention notice in the footer | | | |
| A4 | `docker compose ps` and `curl http://127.0.0.1:8080/health/ready` | `healthy`; `{"status":"ready"}` | | | |
| A5 | From another machine, try to reach port 8080 and port 8000 | Both refused (loopback only) | | | |

### B. Local-only mode ([responsible AI](../governance/responsible-ai.md#local-only-mode))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| B1 | Set `LOCAL_ONLY_MODE=true`, recreate the stack | Workspace states local-only; no consent prompt | | | |
| B2 | Upload a CSV, review the schema, run the analysis | Dashboard, report downloads (Markdown and PDF) | | | |
| B3 | Ask "What limitations or unavailable analysis apply here?" | Answered by the memory route without any model | | | |
| B4 | Ask a data question | Refused or unavailable with a clear message; nothing sent to a provider | | | |

### C. Persistence, backup, and restore ([workspace backup](local-containers.md#backup-and-restore-workspace-state))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| C1 | `docker compose restart api` | Workspace, dataset, analysis, and chat history are still there | | | |
| C2 | Take and verify a workspace backup with the documented commands | `OK: ... workspace(s) ...` | | | |
| C3 | Restore it into a fresh volume (`--replace`), start the API | Same workspace and conversation recovered | | | |
| C4 | Corrupt one byte of the archive and run `verify` | `FAILED` with a checksum message | | | |

### D. Audit verification, rotation, and recovery ([audit and observability](../governance/audit-and-observability.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| D1 | `docker compose exec api python -m scripts.verify_audit --require-files` | Every tenant `OK`, including `system` | | | |
| D2 | `.\ops\backup.ps1` then `.\ops\restore.ps1 <archive>` | Chain verified before the API restarts | | | |
| D3 | `python -m scripts.audit_maintenance status` | Segment and record counts, `chain OK` | | | |
| D4 | Edit one line of an audit file, rerun D1 | `BROKEN` for that tenant, non-zero exit | | | |

### E. Upgrade and rollback ([migration and rollback](migration-and-rollback.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| E1 | Follow the upgrade procedure from a previous tag to this commit | Stack healthy, smoke test passes, audit verifies | | | |
| E2 | Follow the rollback procedure | Previous version healthy; the document's stated data effects are accurate | | | |

### F. Governance and evaluation procedures ([evaluation](../governance/evaluation.md), [model and prompt change policy](../governance/model-and-prompt-change-policy.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| F1 | `python -m scripts.run_evaluations` | `Gates: PASSED` | | | |
| F2 | Change one word in a prompt builder (for example `build_rag_prompt`), run `pytest tests/test_prompt_registry.py` | Fails and tells you how to record the change | | | |
| F3 | Follow the policy to record that change (new hash, version, reason, approver) and rerun | Passes; the procedure was followable from the document alone | | | |
| F4 | Revert the edit | Registry check passes again | | | |

### G. Troubleshooting ([incident runbook](incident-debugging.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| G1 | Cause an error in the UI (upload an unsupported file) and copy the request ID | The error shows a request ID | | | |
| G2 | Find that request in the logs using the runbook's command | One or more JSON lines with the same `request_id` | | | |
| G3 | Stop the API container and open the UI | A clear "unavailable" state; the runbook's recovery steps work | | | |

### H. Privacy and data lifecycle ([data classification](../governance/data-classification.md))

| # | Step | Expected | Result | Evidence | Notes |
|---|---|---|---|---|---|
| H1 | Click **Export my data**, open the ZIP | Manifest, uploads, history, reports; no secrets or other tenants' data | | | |
| H2 | Click **Reset workspace** | Workspace and files gone (`find /data/api` shows none); audit shows `workspace.deleted` | | | |
| H3 | Search the logs and audit file for a distinctive word from your uploaded data | Not found | | | |

## Defects and documentation gaps

| ID | Step | Severity (blocker / major / minor / wording) | What happened | Suggested fix |
|---|---|---|---|---|
| | | | | |

## Outcome

| Field | Value |
|---|---|
| Steps passed / failed / blocked / unclear | |
| Could the tester complete A to H using only the documents? | |
| Blocking defects | |
| Tester sign-off (name, date) | |

File the completed record as a comment on issue #11 and open one issue per blocker or major defect. #11 can
close when blockers are fixed, the failed or unclear steps are resolved or consciously accepted, and the
diagrams in [system-overview.md](../architecture/system-overview.md) have been confirmed against the
repository by the tester.
