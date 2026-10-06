"""Natural-language to safe SQL agent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import monotonic

import pandas as pd

from src.llm.base import LLMClient
from src.llm.observability import llm_purpose
from src.storage.query_executor import (
    QueryTimeoutError,
    UnsafeQueryError,
    execute_read_query,
    validate_read_query,
)
from src.storage.sqlite_store import StoredTable
from src.utils.pii_redaction import redact_pii


class SQLAgentError(RuntimeError):
    """Raised when the SQL agent cannot answer safely."""


@dataclass(frozen=True)
class SQLAgentResult:
    question: str
    sql: str
    result: pd.DataFrame
    llm_text: str
    execution_seconds: float = 0.0
    correction_attempted: bool = False


def answer_with_sql(
    question: str,
    stored_table: StoredTable,
    llm_client: LLMClient,
    conversation_context: list[tuple[str, str | None]] | None = None,
) -> SQLAgentResult:
    """Generate, validate, and execute SQL for a business question."""
    if not question.strip():
        raise SQLAgentError("Question cannot be empty.")

    prompt = build_sql_prompt(
        question=question,
        stored_table=stored_table,
        conversation_context=conversation_context,
    )
    with llm_purpose("sql_generation"):
        llm_response = llm_client.generate(prompt)
    sql = extract_sql(llm_response.text)
    safe_sql = validate_generated_sql(sql)
    execution_seconds = 0.0
    correction_attempted = False
    started_at = monotonic()
    try:
        result = execute_sql(stored_table, safe_sql)
    except (UnsafeQueryError, QueryTimeoutError):
        raise
    except Exception as exc:
        execution_seconds += monotonic() - started_at
        correction_attempted = True
        retry_prompt = build_sql_retry_prompt(
            original_prompt=prompt,
            failed_sql=safe_sql,
            error=str(exc),
        )
        with llm_purpose("sql_correction"):
            retry_response = llm_client.generate(retry_prompt)
        retry_sql = validate_generated_sql(extract_sql(retry_response.text))
        started_at = monotonic()
        try:
            result = execute_sql(stored_table, retry_sql)
        except (UnsafeQueryError, QueryTimeoutError):
            raise
        except Exception as retry_exc:
            raise SQLAgentError(
                "The generated SQL could not be executed after one correction attempt."
            ) from retry_exc
        safe_sql = retry_sql
        llm_response = retry_response

    execution_seconds += monotonic() - started_at
    return SQLAgentResult(
        question=question,
        sql=safe_sql,
        result=result,
        llm_text=llm_response.text,
        execution_seconds=execution_seconds,
        correction_attempted=correction_attempted,
    )


def generate_sql(
    prompt: str, llm_client: LLMClient, purpose: str = "sql_generation"
) -> tuple[str, str]:
    """Generate SQL and return the extracted statement plus original model text."""
    with llm_purpose(purpose):
        response = llm_client.generate(prompt)
    return extract_sql(response.text), response.text


def validate_generated_sql(sql: str) -> str:
    """Apply the shared read-query guard to generated or edited SQL."""
    return validate_read_query(sql)


def execute_sql(stored_table: StoredTable, safe_sql: str) -> pd.DataFrame:
    """Execute already validated SQL against the configured table boundary."""
    return execute_read_query(
        stored_table.database_path,
        safe_sql,
        allowed_tables={stored_table.table_name},
        allowed_columns={stored_table.table_name: set(stored_table.columns)},
        encryption_key=stored_table.encryption_key,
    )


def build_sql_prompt(
    question: str,
    stored_table: StoredTable,
    conversation_context: list[tuple[str, str | None]] | None = None,
) -> str:
    """Build a constrained SQL-generation prompt."""
    columns = "\n".join(
        _format_column_context(column, stored_table) for column in stored_table.columns
    )
    history = _format_conversation_context(conversation_context)
    canonical_mapping = (
        "\n".join(
            f'- {field}: "{column}"' for field, column in stored_table.canonical_mapping.items()
        )
        or "- No canonical business fields were confirmed."
    )
    return f"""You are a careful SQLite analyst.

Generate exactly one read-only SQLite SELECT query that answers the user's question.

Rules:
- Use only the table named {stored_table.table_name}.
- Use only the columns listed below.
- Double-quote every table and column identifier.
- Return SQL only. Do not include explanation.
- Do not use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, or VACUUM.
- Prefer LIMIT 100 unless the user asks for a smaller result.
- If aggregation is useful, use clear column aliases.

Table: {stored_table.table_name}
Row count: {stored_table.row_count:,}
Columns:
{columns}
User-confirmed canonical mapping:
{canonical_mapping}
{history}

Question:
{question}
"""


def build_sql_retry_prompt(original_prompt: str, failed_sql: str, error: str) -> str:
    """Ask for one corrected query using a bounded database error message."""
    return f"""{original_prompt}

The first query failed.
Failed SQL: {failed_sql}
SQLite error: {error[:500]}

Correct the query. Return exactly one read-only SQLite query and no explanation.
"""


def summarize_sql_result(result: pd.DataFrame) -> str:
    """Build a deterministic plain-language summary of a query result."""
    if result.empty:
        return "The query ran successfully but returned no matching rows."
    columns = ", ".join(str(column) for column in result.columns)
    summary = (
        f"Returned {len(result):,} row(s) across {len(result.columns):,} column(s): {columns}."
    )
    if len(result) == 1:
        values = ", ".join(
            f"{column}={value}" for column, value in result.iloc[0].to_dict().items()
        )
        summary += f" Result: {values}."
    return summary


def _format_column_context(column: str, stored_table: StoredTable) -> str:
    dtype = stored_table.column_types.get(column, "unknown")
    samples = stored_table.sample_values.get(column, [])
    sample_text = (
        ", ".join(repr(redact_pii(value)) for value in samples)
        if samples
        else "no non-null samples"
    )
    date_hint = (
        " (ISO date/time)" if samples and all(_looks_iso_date(value) for value in samples) else ""
    )
    return f'- {column} (quote as "{column}"): {dtype}{date_hint}; samples: {sample_text}'


def _format_conversation_context(
    conversation_context: list[tuple[str, str | None]] | None,
) -> str:
    if not conversation_context:
        return ""
    lines = ["Recent questions and SQL (context only):"]
    for question, sql in conversation_context[-3:]:
        lines.append(f"- Question: {redact_pii(question)}")
        if sql:
            lines.append(f"  SQL: {redact_pii(sql)}")
    return "\n".join(lines)


def _looks_iso_date(value: str) -> bool:
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?", value) is not None


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
    match = re.search(r"\b(?:select|with)\b", text, flags=re.IGNORECASE)
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
    candidate = _split_at_statement_terminator(candidate)
    return candidate


def _split_at_statement_terminator(text: str) -> str:
    """Return text through the first semicolon outside a quoted value."""
    delimiter: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if delimiter is None and character in {"'", '"', "`"}:
            delimiter = character
        elif delimiter is not None and character == delimiter:
            if index + 1 < len(text) and text[index + 1] == delimiter:
                index += 1
            else:
                delimiter = None
        elif delimiter is None and character == ";":
            return text[:index].strip()
        index += 1
    return text.strip()
