# Repository Guidance

## Project Purpose

This is a conversational business intelligence application for uploaded CSV,
Excel, and PDF files. It combines deterministic analytics, safe SQL querying,
document retrieval, anomaly detection, visualization, and report generation.

## Architecture

- `app.py` is the Streamlit entry point.
- `src/ui/` contains upload, dashboard, and chat interfaces.
- `src/ingestion/` loads tabular files and PDFs.
- `src/profiling/` handles schema mapping, profiling, and capability detection.
- `src/analytics/` provides deterministic analytics and anomaly detection.
- `src/storage/` stores tabular data in SQLite and executes validated queries.
- `src/documents/` handles chunking, embeddings, ChromaDB storage, and retrieval.
- `src/agents/` contains the SQL, RAG, and reporting workflows.
- `src/orchestration/` routes questions through SQL, document RAG, memory, or fallback paths with LangGraph.
- `src/memory/` manages session-level conversational context.
- `src/reporting/` produces PDF reports.
- `tests/` contains the pytest suite.

## Technology

- Python and Streamlit
- LangGraph for routing and orchestration
- Gemini 2.5 Flash for SQL generation and grounded document answers
- SentenceTransformers `all-MiniLM-L6-v2` for embeddings
- ChromaDB for local vector storage
- SQLite for uploaded tabular data
- Plotly and ReportLab for visualization and reports

## Development Commands

Create and activate a virtual environment before installing dependencies.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run quality checks with:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy config src tests
```
