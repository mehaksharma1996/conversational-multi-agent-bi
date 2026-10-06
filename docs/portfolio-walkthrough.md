# Portfolio walkthrough: where the system is deterministic, where a model helps, and what can still go wrong

Written for a reader evaluating the engineering, not just the demo. Every claim names the code or test that
backs it, and the last section lists what is **not** solved. Diagrams are in the
[system overview](architecture/system-overview.md).

## What it is

A local conversational business-intelligence workbench. An analyst uploads CSV or Excel tables and PDF policy
documents, reviews an inferred schema, and gets deterministic analytics, anomaly review candidates, a
human-reviewed classifier, guarded question answering over the table, cited answers from the documents, and
combined "which rows violate this policy" lookups, with reports and exports. It is decision support: nothing a
model says can approve, reject, delete, or export anything.

## The design rule

> Put every guarantee in code that cannot be talked out of it. Let a model help only where a wrong answer is
> caught, shown for review, or harmless.

| Concern | Deterministic enforcement (does not depend on the model) | Bounded model assistance (checked before it matters) |
|---|---|---|
| What SQL runs | One `SELECT`/CTE, keyword, table, column, and function allowlists, SQLite's authorizer, a read-only connection, a 500-row cap, a 5 s deadline (`src/storage`, evaluated by `evals/v1`) | The model *drafts* the SQL; an invalid draft is refused or gets one bounded correction that is re-validated; an optional human approval shows the SQL before it runs |
| Whether to call a model at all | Consent gate, `LOCAL_ONLY_MODE`, per-provider disclosure, PII redaction of samples and excerpts | Routing and criteria extraction return schema-validated JSON with at most one repair |
| What a document answer may claim | Relevance gate (off-topic questions are refused with **no model call**), citations validated against retrieved chunks, quoted text must appear verbatim | The model writes the answer from excerpts that are labelled untrusted |
| Analytics and anomalies | Fixed-seed, reproducible pipelines; results shown with their limitations; the classifier excludes identifiers and target proxies and reports holdout metrics | None; no model is involved |
| Tenant isolation | Every lookup is tenant-scoped, foreign resources return the same 404 as missing ones (tests and an isolation evaluation) | None |
| What gets recorded | Allowlist-only telemetry and audit; content, identity, and secrets cannot be added | None |
| Persistence and recovery | Checksummed uploads, versioned migrations, deterministic rebuild after restart, verified backups (ADR 0022) | None |

The pattern is visible in the code: model calls sit behind a small interface (`src/llm`), a failing or
adversarial provider degrades to a documented fallback (`failure_recovery` evaluation cases), and the
guards run on the *output*, not on trust in the prompt.

## A five-minute demo path

1. `docker compose up -d --build --wait`, open `http://127.0.0.1:8080` (see [local containers](operations/local-containers.md)).
2. Upload a CSV, review the schema mapping, run the analysis: nothing here calls a model.
3. Ask a question in local-only mode (`LOCAL_ONLY_MODE=true`): the session-memory route answers deterministically.
4. With a provider configured, accept the consent notice and ask a data question: the UI shows the generated SQL,
   the rows, and how the answer was checked.
5. Upload a PDF and ask a policy question: the answer cites its sources; ask something off-topic and it refuses
   without calling the model.
6. Restart the API (`docker compose restart api`): the workspace, dataset, conversation, and index come back.
7. Click **Export my data**, then **Reset workspace**; check the audit log with
   `docker compose exec api python -m scripts.verify_audit`.

## How it is verified

- `python -m scripts.check_all` runs lint, formatting, types, the full test suite, and the offline evaluation gate.
- The **offline evaluation** (`python -m scripts.run_evaluations`) runs versioned fixtures through the real guards,
  retrieval, and orchestration with a *scripted* model, so it proves the system's reactions to good, bad, and
  adversarial model behaviour, including negative controls that show each check can fail.
- CI also builds and scans the container images, runs the full Compose topology (journeys, restart recovery,
  backup/restore, no CSP violations), runs the pgvector contract against real Postgres, enforces a bundle-size
  budget, and checks that the OpenAPI contract stays compatible.
- Decisions and their evidence are in the [ADRs](adr/README.md); controls and their gaps are in the
  [threat model](security/threat-model.md).

## Residual risks, stated plainly

- **Safe is not correct.** A valid read-only query can answer the wrong question. The UI shows the SQL and rows
  so a person can check; the evaluation measures required columns and rows for fixed cases only.
- **The offline gate does not measure real-model quality.** It proves reactions, not accuracy. Real-model
  evaluation is #14 and needs credentials.
- **Prompt injection is mitigated, not eliminated.** Document text is labelled untrusted and outputs are
  validated, but a model can still be misled into a wrong, cited answer.
- **Redaction is regex-based** and does not cover names, addresses, free-text identifiers, or the typed question.
- **The vector index is not encrypted** (ADR 0021, a time-bounded waiver); conversation content is encrypted only
  when `APP_ENCRYPTION_KEY` is set.
- **One node, one process.** There is no multi-process coordination, and non-loopback exposure stays prohibited
  until a live identity-provider check passes (#9).
- **Audit is tamper-evidence, not immutability.** Anyone with filesystem access can rewrite a chain; archive
  rotated segments off-host if history must be provable.
- **Not independently exercised yet.** The operations and governance documents were written and tested by their
  author; [independent validation](operations/independent-validation.md) is the open step in #11.
