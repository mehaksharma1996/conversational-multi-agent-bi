# Conversational Business Intelligence Workbench

A Streamlit application for exploring uploaded CSV, Excel, and PDF files through
deterministic analytics, read-only SQL, document retrieval, and coordinated
conversational workflows.

## What It Does

- Profiles uploaded tabular data and proposes a reviewable canonical schema.
- Runs deterministic summaries, trends, category breakdowns, and Plotly charts.
- Flags unusual numeric patterns with a configurable Isolation Forest model.
- Persists each browser session to its own SQLite and ChromaDB workspace.
- Generates Gemini-backed SQL and executes it through table, column, function,
  row, and execution-time guardrails.
- Retrieves page-aware PDF context with relevance filtering.
- Routes questions through SQL, document RAG, session memory, or a hybrid
  document-plus-data workflow using LangGraph.
- Produces a deterministic business report with on-demand PDF export.

Anomaly results are review candidates, not confirmed fraud or misconduct. The
application is a decision-support workbench, not an autonomous decision maker.

## Architecture

```text
CSV / Excel ──> profile ──> reviewed schema ──> analytics/charts/anomalies
      │                                      └─> session SQLite ──> SQL workflow
      │
PDFs ──> page chunks ──> local embeddings ──> session ChromaDB ──> RAG workflow
                                                                    │
Question ──> LangGraph router ──> memory / SQL / RAG / hybrid ──────┘
                                      │
                                      └─> displayed evidence + on-demand report
```

The SQL and RAG components are specialized workflows coordinated by a router.
For questions that explicitly combine uploaded transactions with document
guidance, the hybrid route retrieves the guidance first and then generates a
guarded query using that context. It does not claim autonomous agent consensus.

## Local Setup

Python 3.12 through 3.14 is supported. The current verified environment uses
Python 3.14.

```powershell
py -3.14 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.lock
Copy-Item .env.example .env
& ".\.venv\Scripts\python.exe" -m streamlit run app.py
```

For development tools:

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
```

Set `GEMINI_API_KEY` in `.env` to enable SQL generation and document answers.
The deterministic dashboard, analytics, and report structure do not require an
API key.

The first PDF upload may download the configured SentenceTransformer model.

To try the dashboard immediately, upload `sample_data/transactions.csv` and
`sample_data/review_policy.pdf`.

Resource and retention limits can be configured with `MAX_TABULAR_UPLOAD_BYTES`,
`MAX_TABULAR_ROWS`, `MAX_PDF_UPLOAD_BYTES`, `MAX_TOTAL_PDF_BYTES`,
`MAX_PDF_PAGES`, `MAX_DOCUMENT_CHUNKS`, `RETRIEVAL_TOP_K`,
`RETRIEVAL_MAX_DISTANCE`, and `SESSION_RETENTION_HOURS`. `SQLITE_DB_PATH` and
`CHROMA_PERSIST_DIR` define the non-session defaults; browser sessions are
intentionally stored below `APP_DATA_DIR/sessions/<session-id>` for isolation.

## Quality Checks

```powershell
& ".\.venv\Scripts\python.exe" -m ruff check .
& ".\.venv\Scripts\python.exe" -m ruff format --check .
& ".\.venv\Scripts\python.exe" -m mypy config src tests
& ".\.venv\Scripts\python.exe" -m pytest --cov --cov-report=term-missing
```

CI runs the same checks on pushes and pull requests. `requirements.txt` pins
direct runtime dependencies; `requirements.lock` captures the fully resolved,
tested environment.

## Example Questions

```text
Show the top five merchants by total amount.
What analysis is possible with this dataset?
Show me the SQL used for the previous question.
According to the uploaded policy, which transaction types require escalation?
Which uploaded transactions appear to match the escalation policy?
```

The final question activates the hybrid route when both table data and document
context are available.

## Data and Security Boundaries

- Uploaded table and vector data are isolated under a random session directory.
- Removing an upload clears its derived state; **Reset session data** clears the
  complete current workspace.
- SQLite is opened read-only for user queries and constrained to the current
  session table and columns.
- Query results are capped in SQL and execution is interrupted after a deadline.
- Upload size, row, PDF page, and document chunk limits protect local resources.
- `.env`, session databases, vector indexes, reports, and test artifacts are
  excluded from Git.

Gemini receives the user question and schema information for SQL generation. For
document questions, the retrieved PDF excerpts are also sent to Gemini. Do not
upload sensitive material unless this data flow is acceptable for your use case.

This project does not currently provide authentication, authorization, encrypted
storage, audit logging, or enterprise retention controls. Add those controls
before hosting it for untrusted users.

## Repository Layout

- `app.py`: Streamlit entry point and session initialization.
- `src/ui/`: upload, dashboard, report, and chat interfaces.
- `src/profiling/`: profiling, schema mapping, and capability readiness.
- `src/analytics/`: deterministic analytics and anomaly detection.
- `src/storage/`: per-session SQLite persistence and guarded querying.
- `src/documents/`: page-aware chunking, embeddings, retrieval, and ChromaDB.
- `src/agents/`: SQL, document-answering, and report workflows.
- `src/orchestration/`: LangGraph routing and hybrid coordination.
- `src/memory/`: state lifecycle and conversational memory.
- `tests/`: unit and Streamlit integration tests.

## Current Limitations

- Schema mapping remains heuristic until confirmed by the user.
- Retrieval relevance is model- and document-dependent.
- Hybrid policy-to-SQL translation must be reviewed before operational use.
- Scanned PDFs require OCR before upload.
- Gemini availability, quotas, and responses are external dependencies.
