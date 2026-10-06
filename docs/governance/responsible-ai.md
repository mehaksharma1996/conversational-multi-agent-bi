# Responsible AI notes

This document states what the workbench is for, what it does to reduce harm, and
where it can still be wrong. Each section separates **implemented controls**
(present in the code and covered by tests or evaluations) from **recommendations**
(not implemented). It describes a local, single-node application; it is not a
compliance certification and makes no claim of regulatory suitability.

## Intended use

Exploratory business analysis of CSV/Excel tables and PDF policy documents by an
analyst who can inspect the evidence: schema review, deterministic summaries,
anomaly *review candidates*, guarded question answering over the uploaded table,
grounded answers from uploaded PDFs, and combined "policy-to-rows" lookups.

It is **decision support**. It is not intended to make or automate decisions about
people (credit, employment, benefits, fraud adjudication), to act on data, or to
be the sole basis for any consequential action.

## Human review requirements

Implemented:

- Schema mapping must be confirmed by the user before analysis or chat; low-confidence
  mappings are shown with confidence and reason.
- Every SQL and hybrid answer shows the generated SQL and result rows so a person can
  check the logic.
- The web UI labels each answer with its route and how it was checked
  (`AnswerProvenance`), states "a person should review the evidence before acting" for
  SQL, RAG, and hybrid answers, and shows the request ID for support.
- Anomaly results are described as review candidates with the analytics limitations
  returned by the API.
- No model output can authorize, mutate, delete, or export anything. Consequential
  operations (reset, delete, export, report) are deterministic API actions the user
  triggers explicitly.

Recommendation: for any downstream use that affects people, add an organizational
review step with named reviewers; the application cannot enforce one.

## Known limitations

- Schema mapping is heuristic until confirmed.
- Model output can be wrong, incomplete, or confidently phrased when it is wrong.
- Retrieval relevance depends on the embedding model and document quality.
- Results are only as good as the uploaded data (missing values, duplicates,
  mislabeled columns).
- Gemini availability, quota, and behavior are external and can change.
- Scanned PDFs are rejected; there is no OCR.

## Anomaly-detection limitations

Implemented: a deterministic rule set plus an Isolation Forest, reported separately, with
a user-controlled contamination fraction and the limitations text returned with results.

Limitations:

- A flag means "unusual relative to this file", not "fraudulent" or "wrong". Legitimate
  rare events are flagged; unusual-but-abusive patterns that look normal are missed.
- `contamination` fixes roughly how many rows are flagged; it is not a calibrated
  probability. Changing it changes the results.
- Small files, single-feature files, and files dominated by one segment produce weak signals.
- There is no labelled evaluation of anomaly precision or recall. The offline evaluation
  suite does not score anomaly quality.

## Supervised-classification limitations

Implemented: classification runs only after the user confirms a binary label mapping and the
file has at least 100 labelled rows, at least 20 rows in each class, and a usable feature. The
pipeline excludes identifiers, dates, constants, and deterministic target proxies; compares a
class-weighted logistic regression with a class-weighted gradient-boosted tree using stratified
training folds; chooses its threshold from out-of-fold training predictions; and reserves a
stratified holdout for precision, recall, F1, ROC-AUC, PR-AUC, and confusion-matrix reporting.
PR-AUC is the primary selection metric. The ranked output contains source-row references and
scores only, and is explicitly presented as a human-review queue.

Limitations:

- Historical labels can encode bias, inconsistent decisions, or data-quality errors. The workbench
  does not infer whether a label is fair or suitable for the proposed use.
- Leakage detection catches identifiers and simple deterministic proxies, not every indirect,
  temporal, or operational leak. A domain reviewer must inspect the selected features.
- A holdout from one uploaded file does not establish future performance, calibration, fairness,
  causation, or fitness for a consequential decision.
- The selected threshold maximizes training-fold F1. It is not a policy threshold and must be
  revalidated against business costs and new data.
- Scores and predicted classes must not automatically approve, reject, accuse, prioritize services,
  or otherwise determine an outcome for a person.

## Text-to-SQL risks and controls

Implemented controls (each exercised by evaluation cases, with negative controls that
prove the checks can fail):

- The model output must be exactly one `SELECT`/CTE statement; the first statement is
  extracted and any tail is discarded, never executed.
- Blocked keywords, a table allowlist, a column allowlist, and a function allowlist,
  enforced again by SQLite's authorizer at execution time.
- A read-only connection, a 500-row cap, and a 5-second deadline.
- One bounded correction attempt for execution errors; the corrected SQL is re-validated.
- Hybrid questions can opt into a human approval interrupt after validation and before execution.
  Approval and optional edits pass through the same guard again; rejection produces no rows.
- Sample values shown to the model are PII-redacted, and can be omitted entirely with
  `GEMINI_EXCLUDE_SAMPLE_VALUES`.
- Route classification and hybrid criteria extraction validate against strict schemas; a
  malformed output receives at most one repair attempt that reports only a validation category.
  Repair and validation failure counts are recorded without retaining output content.

Residual risks:

- **Safe is not correct.** A valid read-only query can answer the wrong question
  (wrong column, missing filter, wrong grouping). The evaluation suite checks required
  columns, fragments, and expected rows for fixed cases, which is evidence about those
  cases only.
- Ambiguous questions get one interpretation; the model does not ask for clarification.
- The row cap silently truncates large results.
- Approval improves reviewability, not semantic correctness. A reviewer must still check that the
  proposed SQL implements the cited policy and intended business question.

## RAG grounding and citation limitations

Implemented: retrieved excerpts are PII-redacted and framed as untrusted source material;
citation numbers are validated against the retrieved set; quoted passages of 15+
characters must appear verbatim in a retrieved chunk; answers with no citation are
reported as `uncited`; retrieval beyond a distance threshold is rejected and the model
is **not called** when no evidence remains; grounding warnings are appended to the answer
and returned in structured `provenance`.

Limitations:

- These are structural checks. A correctly numbered citation can still attach to a claim
  the source does not support, and paraphrases are not verified.
- The distance threshold was calibrated on a small corpus with one embedding model; other
  corpora may need tuning (`RETRIEVAL_MAX_DISTANCE`).
- Instructions hidden in documents are mitigated by prompt framing and downstream
  validation, not eliminated. The evaluation suite includes adversarial documents, but a
  scripted model cannot prove how a real model will behave.

## Hybrid-route provenance limitations

Implemented: a model extracts criteria through a strict schema limited to six keys; malformed
outputs get at most one bounded repair and otherwise fall back to excerpt provenance. String
values that do not literally appear in the retrieved excerpts are dropped;
size is bounded; the result reports `traced`, `unreferenced`, `no_structured_criteria`, or
`excerpt_fallback`; if the generated SQL is refused the route degrades to the document answer
with an explicit notice.

Limitations:

- Tracing is literal substring matching for strings; numbers are not traced to evidence.
- "SQL references the criteria" is literal value overlap. It does not show the SQL
  implements the policy correctly.
- The final rows are candidates that need human review against the cited excerpts.

## Data sent to Gemini

When Gemini is configured and the workspace has accepted the notice, these can be sent:

| Data | Redaction |
|---|---|
| Your current question text | **None** (sent as written) |
| Table name, column names, types, row count, confirmed schema mapping | None |
| Up to five sample values per column | Regex PII redaction; omitted with `GEMINI_EXCLUDE_SAMPLE_VALUES=true` |
| Up to three prior questions and SQL | Regex PII redaction |
| Retrieved document excerpts (hybrid criteria extraction uses the first 800 characters per chunk) | Regex PII redaction |
| SQLite error text (first 500 characters) on a correction attempt | None |

Query result rows are **not** sent to the model; the answer summary is built
deterministically. Google's retention terms are Google's, not this project's; see the README.

## Consent behavior

Implemented: while Gemini is configured, the API returns `409 gemini_consent_required` for
questions until the workspace records acceptance of a versioned notice; the acceptance
(notice version, no content) is written to the audit stream. Consent is per workspace and lives
in process memory with the workspace record.

Recommendation: consent is not re-requested when the notice text changes, and it is not tied to
a verified user identity beyond the local-development tenant.

## Local-only mode

`LOCAL_ONLY_MODE=true` withholds every hosted provider's credentials (Gemini, Anthropic, and an Ollama endpoint
that is not local) at client construction, even if configured, so SQL generation, document answers, hybrid queries,
and model-based routing cannot reach a hosted service. Deterministic analytics, profiling, anomaly review, reports,
exports, and session-memory answers continue to work. This is the only complete guarantee that content is not
sent to a hosted provider.

A **local model** refines this without weakening it: with `LLM_PROVIDERS=ollama` and `OLLAMA_MODEL`, model-backed
questions also work in local-only mode, but only if the Ollama endpoint is loopback or a host the operator declared
container-internal in `OLLAMA_TRUSTED_HOSTS`. Any other endpoint counts as a hosted recipient and is withheld in
local-only mode. The declaration is an operator assertion; the application cannot prove where a hostname routes.
A local model still processes prompts built from your data, so its output carries the same grounding and
guardrail checks as any other provider, and its quality can differ.

### Providers, fallback, and consent

Providers are configured on the server only (`LLM_PROVIDERS`, ordered). If a provider fails after its own bounded
retries, the next configured provider is tried once; credentials withheld for a provider (for example in local-only
mode) are never used. Redaction and `GEMINI_EXCLUDE_SAMPLE_VALUES` apply to the prompt before any provider sees it,
so every provider in a chain receives the same minimised content. The consent notice names every hosted provider
in the chain (`data_recipients`), consent records which providers were disclosed, and a configuration that adds a
hosted provider requires consent again before model-backed questions run. Audit and telemetry record the provider
and model that actually answered and whether a fallback occurred, never prompts or answers.

## PII-redaction limitations

Redaction is regex-based and best effort: it catches e-mail addresses, SSN-shaped numbers,
phone-shaped numbers, and Luhn-valid card numbers. It does not catch names, addresses, account
numbers without a checksum, free-text identifiers, or non-US formats. It is not applied to the
question text you type, to values displayed to you, or to files stored locally. Treat it as
defense in depth, not as anonymization. Evaluations confirm the patterns above do not reach the
prompt; they do not measure recall on real data.

## Retention and deletion behavior

Implemented:

- API workspaces expire after `SESSION_RETENTION_HOURS` of inactivity (sliding window).
  Expiry is checked lazily on access and swept whenever a new workspace is created;
  `workspace.expired` is audited with reason `retention_expired`.
- `DELETE /api/v1/workspaces/{id}` removes the workspace's in-memory records, closes its
  vector index, and deletes its directory; `workspace.deleted` is audited.
- Streamlit sessions have their own heartbeat-based cleanup and deletion log.
- Audit events are append-only and contain no content, so they are intentionally *not* removed
  with the workspace.

Limitations:

- API resource metadata is process-local. A restart loses every record, and workspace directories
  under `APP_DATA_DIR/api/` from before the restart become orphaned until removed manually.
- Chroma vector storage is not encrypted at rest (time-bounded waiver and compensating controls:
  [ADR 0021](../adr/0021-vector-store-encryption-waiver.md); use host disk encryption).
- There is no retention or rotation policy for the audit files.
- Deletion is not independently verified beyond the directory no longer existing.

## Evaluation methodology

See [evaluation.md](evaluation.md). In short: versioned synthetic fixtures run through the real
guard, retrieval, and orchestration code with a scripted model and a deterministic embedder;
evaluators check properties (route, SQL validity and semantics, evidence, citations, grounding,
criteria provenance, redaction, tenant isolation); safety checks are hard failures. Passing
proves those behaviors for those cases; it does not measure real-model accuracy.

## Audit and observability behavior

See [audit-and-observability.md](audit-and-observability.md). In short: FastAPI operations emit
allowlist-only structured telemetry and append-only, hash-chained, per-tenant audit events with no
questions, SQL, rows, document text, prompts, or secrets. There is no production telemetry exporter.

## Incident and debugging guidance

See [the incident runbook](../operations/incident-debugging.md). Start from the request ID shown
to the user; do not enable `DEBUG_LOG_RAW_CONTENT` on shared machines.

## Implemented versus recommended

| Area | Implemented | Recommended, not implemented |
|---|---|---|
| Review | Confirmation steps, visible SQL/sources, anomaly/classifier review notices | Organizational reviewer sign-off |
| SQL safety | Parser + allowlists + authorizer + read-only + caps | Semantic verification against a data dictionary |
| Grounding | Citation/quote checks, refusal without evidence | Claim-level entailment checking |
| Privacy | Consent, redaction, local-only mode, allowlist telemetry | Question redaction, vector-store encryption, DLP tooling |
| Audit | Append-only hash-chained per-tenant events | Off-host WORM storage, rotation, access controls on the audit file |
| Retention | Sliding expiry, explicit delete | Durable metadata, orphan sweep, verified deletion |
| Evaluation | Offline gated suite, classifier holdout metrics, opt-in live run | External validation, fairness studies, anomaly precision/recall studies |
| Telemetry | Structured events, correlation IDs | Metrics/trace exporter, alerting, SLOs |
