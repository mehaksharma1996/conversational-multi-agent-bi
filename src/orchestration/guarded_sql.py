"""Guarded SQL steps shared by the question graph and future investigation specialists (ADR 0024).

``GuardedSQLService`` binds one confirmed ``StoredTable`` and one model client to the existing
SQL primitives: prompt building, generation, read-only validation, policy-to-SQL verification,
execution under the table/column allowlist and row and time limits, and the single correction
prompt. It adds no validation or execution path of its own, and it holds no state between calls.

Scope of its guarantees:

- It is not an authorization boundary. Callers must hand it a table already resolved for the
  authenticated tenant and workspace, after consent and capability checks (``apps/api``). For
  investigations, the INV-3 tool gateway remains responsible for tenant binding, consent,
  capability, budget, and cancellation before any call reaches this service.
- It never executes unvalidated SQL on its own initiative, and it has no approval step. Callers
  decide where human approval interrupts happen and must send any edited SQL back through
  ``validate`` (and ``verify_policy`` for document-derived criteria) before ``execute``.
- ``execute`` classifies failures but never retries. The caller counts corrections and asks for at
  most ``MAX_SQL_CORRECTIONS`` of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

import pandas as pd

from src.agents.sql_agent import (
    build_sql_prompt,
    build_sql_retry_prompt,
    execute_sql,
    generate_sql,
    validate_generated_sql,
)
from src.llm.base import LLMClient
from src.orchestration.policy_grounding import PolicySQLVerification, verify_policy_sql
from src.storage.query_executor import QueryTimeoutError, UnsafeQueryError
from src.storage.sqlite_store import StoredTable

MAX_SQL_CORRECTIONS = 1
MAX_ERROR_CHARS = 500

SQLExecutor = Callable[[StoredTable, str], pd.DataFrame]
ExecutionStatus = Literal["succeeded", "refused", "retryable", "exhausted"]


@dataclass(frozen=True)
class SQLExecution:
    """What one execution attempt produced.

    - ``succeeded``: ``dataframe`` holds the bounded result.
    - ``refused``: the guard rejected the query or it timed out. Never correct it.
    - ``retryable``: a database error; ``error_text`` (bounded) may feed one correction prompt.
    - ``exhausted``: a database error after the allowed corrections were used.

    ``error`` keeps the original exception so callers can re-raise it unchanged.
    """

    status: ExecutionStatus
    elapsed_seconds: float
    dataframe: pd.DataFrame | None = None
    error: BaseException | None = None
    error_text: str = ""


class GuardedSQLService:
    def __init__(
        self,
        stored_table: StoredTable,
        llm_client: LLMClient,
        *,
        executor: SQLExecutor = execute_sql,
    ) -> None:
        self.stored_table = stored_table
        self.llm_client = llm_client
        self._executor = executor

    def prompt(
        self,
        question: str,
        conversation_context: list[tuple[str, str | None]] | None = None,
    ) -> str:
        return build_sql_prompt(question, self.stored_table, conversation_context)

    def generate(self, prompt: str, purpose: str = "sql_generation") -> tuple[str, str]:
        """Return the extracted SQL candidate and the raw model text."""
        return generate_sql(prompt, self.llm_client, purpose)

    def validate(self, candidate: str) -> str:
        """Apply the shared read-only guard to generated or edited SQL; raises UnsafeQueryError."""
        return validate_generated_sql(candidate)

    def verify_policy(
        self,
        validated_sql: str,
        criteria: dict[str, Any],
        *,
        question: str | None = None,
    ) -> PolicySQLVerification:
        """Check validated SQL against document-attributed predicates before it may run."""
        return verify_policy_sql(validated_sql, criteria, user_question=question)

    def execute(self, validated_sql: str, *, corrections_used: int) -> SQLExecution:
        """Run already validated SQL once and classify the outcome; never retries."""
        if corrections_used < 0:
            raise ValueError("corrections_used must not be negative.")
        started_at = monotonic()
        try:
            dataframe = self._executor(self.stored_table, validated_sql)
        except (UnsafeQueryError, QueryTimeoutError) as exc:
            return SQLExecution("refused", monotonic() - started_at, error=exc)
        except Exception as exc:
            elapsed = monotonic() - started_at
            if corrections_used >= MAX_SQL_CORRECTIONS:
                return SQLExecution("exhausted", elapsed, error=exc)
            return SQLExecution(
                "retryable", elapsed, error=exc, error_text=str(exc)[:MAX_ERROR_CHARS]
            )
        return SQLExecution("succeeded", monotonic() - started_at, dataframe=dataframe)

    def correction_prompt(self, original_prompt: str, failed_sql: str, error_text: str) -> str:
        return build_sql_retry_prompt(original_prompt, failed_sql, error_text)
