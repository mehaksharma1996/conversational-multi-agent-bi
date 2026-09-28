"""Natural-language to safe SQL agent."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from src.llm.base import LLMClient
from src.storage.query_executor import execute_read_query, validate_read_query
from src.storage.sqlite_store import StoredTable


class SQLAgentError(RuntimeError):
    """Raised when the SQL agent cannot answer safely."""


@dataclass(frozen=True)
class SQLAgentResult:
    question: str
    sql: str
    result: pd.DataFrame
    llm_text: str


def answer_with_sql(
    question: str,
    stored_table: StoredTable,
    llm_client: LLMClient,
) -> SQLAgentResult:
    """Generate, validate, and execute SQL for a business question."""
    if not question.strip():
        raise SQLAgentError("Question cannot be empty.")

    prompt = build_sql_prompt(question=question, stored_table=stored_table)
    llm_response = llm_client.generate(prompt)
    sql = extract_sql(llm_response.text)
    safe_sql = validate_read_query(sql)
    result = execute_read_query(stored_table.database_path, safe_sql)

    return SQLAgentResult(
        question=question,
        sql=safe_sql,
        result=result,
        llm_text=llm_response.text,
    )


def build_sql_prompt(question: str, stored_table: StoredTable) -> str:
    """Build a constrained SQL-generation prompt."""
    columns = "\n".join(f"- {column}" for column in stored_table.columns)
    return f"""You are a careful SQLite analyst.

Generate exactly one read-only SQLite SELECT query that answers the user's question.

Rules:
- Use only the table named {stored_table.table_name}.
- Use only the columns listed below.
- Return SQL only. Do not include explanation.
- Do not use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, or VACUUM.
- Prefer LIMIT 100 unless the user asks for a smaller result.
- If aggregation is useful, use clear column aliases.

Table: {stored_table.table_name}
Columns:
{columns}

Question:
{question}
"""


def extract_sql(text: str) -> str:
    """Extract a SQL statement from plain text or a Markdown code fence."""
    stripped = text.strip()
    if not stripped:
        raise SQLAgentError("The LLM returned an empty SQL response.")

    fenced = re.search(r"```(?:sql)?\s*(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()

    stripped = _strip_non_sql_prefix(stripped)
    candidate = _first_statement(stripped)
    if not candidate:
        raise SQLAgentError("No SQL query could be extracted from the LLM response.")

    return candidate


def _strip_non_sql_prefix(text: str) -> str:
    match = re.search(r"\bselect\b", text, flags=re.IGNORECASE)
    if not match:
        return text
    return text[match.start() :]


def _first_statement(text: str) -> str:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    candidate = " ".join(lines).strip()
    if ";" in candidate:
        candidate = candidate.split(";", 1)[0].strip()
    return candidate
