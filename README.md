# Conversational Multi-Agent Business Intelligence Platform

A Streamlit-based business intelligence app built in small milestones.

## Current Status

Milestone 15 is implemented:

- Project folder structure
- Python dependencies
- Environment variable example
- Minimal Streamlit app shell
- CSV/Excel upload
- Data preview
- Column summary
- Deterministic data profiling
- Numeric, date-like, categorical, ID, and label candidate detection
- Canonical schema mapping
- Capability detection with clear missing-field explanations
- SQLite persistence for uploaded data
- Safe read-only SQL query execution
- Basic deterministic analytics
- Numeric summaries, categorical breakdowns, amount breakdowns, and trends
- ML-backed anomaly detection with row-level explanations
- Plotly chart generation with reusable chart metadata
- Gemini LLM wrapper using `gemini-2.5-flash`
- Conversational safe SQL agent for uploaded data questions
- PDF ingestion and document chunking
- ChromaDB-backed document retrieval
- Basic Gemini-backed RAG answers for uploaded PDF documents
- LangGraph orchestrator for routing questions to SQL or document RAG
- Deterministic business report generation
- PDF report export using ReportLab
- Embedded Plotly chart images in exported PDF reports
- Session memory for follow-up questions about prior SQL, analysis outputs, charts, reports, and limitations
- Final dashboard status polish and portfolio documentation

## Run Locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app should open in your browser and show the BI workspace.

Upload a `.csv`, `.xls`, or `.xlsx` file from the sidebar.
The dashboard will show preview, profile, capabilities, analytics, charts, anomalies, and SQL tabs.
The dashboard also includes a report tab with a downloadable PDF report containing report text and chart images.
The chat input is enabled when data is uploaded and `GEMINI_API_KEY` is configured.
You can also upload PDF documents in the sidebar and ask document questions.

Useful test questions:

```text
Show me the top 5 merchants by total amount
Show me the SQL used
What analysis was possible?
What data columns were missing?
What are the top risks?
Explain the charts
According to the uploaded policy document, which transactions should be escalated?
```

## Portfolio Summary

This project demonstrates a modular Streamlit BI application that can inspect arbitrary uploaded business data, map available columns into a small canonical schema, determine which analyses are possible, run deterministic analytics and anomaly detection, generate Plotly charts, persist data in SQLite, answer safe SQL-backed questions with Gemini, retrieve policy context from uploaded PDFs with ChromaDB, and export a business report as PDF.

The design intentionally favors transparent, inspectable outputs over hidden automation. If fields are missing, the app reports limitations instead of failing or inventing unavailable analyses.

## Environment

Copy `.env.example` to `.env` when you are ready to add API-backed features.

```bash
cp .env.example .env
```

`GEMINI_API_KEY` is optional until LLM-backed agents are used.

The default Gemini model is:

```bash
GEMINI_MODEL=gemini-2.5-flash
```

The default embedding model is:

```bash
EMBEDDING_MODEL=all-MiniLM-L6-v2
```

## Planned Milestones

1. Project setup and minimal Streamlit shell
2. CSV/Excel upload and preview
3. Data profiling
4. Schema mapping and capability detection
5. SQLite storage and safe query layer
6. Basic analytics
7. ML/anomaly detection
8. Chart generation
9. Gemini LLM wrapper
10. Safe SQL agent
11. PDF ingestion and RAG
12. LangGraph orchestration
13. Report generation
14. Conversational follow-up Q&A
15. Final dashboard polish and documentation
