"""Tests for the natural-language to safe SQL agent."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from src.agents.sql_agent import answer_with_sql, build_sql_prompt, extract_sql
from src.llm.base import LLMResponse
from src.storage.query_executor import UnsafeQueryError
from src.storage.sqlite_store import SQLiteStore
from tests.test_utils import isolated_database_path


@dataclass
class FakeLLM:
    text: str
    provider: str = "fake"
    model: str = "fake-model"

    def generate(self, prompt: str) -> LLMResponse:
        self.last_prompt = prompt
        return LLMResponse(text=self.text, model=self.model, provider=self.provider)


def _stored_table():
    database_path = isolated_database_path("sql_agent")
    dataframe = pd.DataFrame(
        {
            "merchant": ["A", "B", "A"],
            "amount": [10.0, 30.0, 20.0],
        }
    )
    return SQLiteStore(database_path).save_dataframe(dataframe)


def test_extract_sql_from_markdown_fence() -> None:
    text = """Here is the query:

```sql
SELECT merchant, SUM(amount) AS total_amount
FROM uploaded_data
GROUP BY merchant
```
"""

    sql = extract_sql(text)

    assert sql.startswith("SELECT merchant")
    assert "GROUP BY merchant" in sql


def test_extract_sql_ignores_prose_before_query() -> None:
    text = (
        "Sure, here is the SQL:\n"
        "SELECT merchant, SUM(amount) AS total_amount\n"
        "FROM uploaded_data\n"
        "GROUP BY merchant\n"
        "ORDER BY total_amount DESC\n"
        "LIMIT 5;"
    )

    sql = extract_sql(text)

    assert sql.startswith("SELECT merchant")
    assert "Sure" not in sql
    assert sql.endswith("LIMIT 5")


def test_extract_sql_ignores_prose_after_query() -> None:
    text = (
        "SELECT merchant, SUM(amount) AS total_amount "
        "FROM uploaded_data GROUP BY merchant LIMIT 5; "
        "This query returns the top merchants."
    )

    sql = extract_sql(text)

    assert sql == (
        "SELECT merchant, SUM(amount) AS total_amount FROM uploaded_data GROUP BY merchant LIMIT 5"
    )


def test_build_sql_prompt_includes_table_and_columns() -> None:
    stored_table = _stored_table()

    prompt = build_sql_prompt("Top merchants?", stored_table)

    assert "uploaded_data" in prompt
    assert "- merchant" in prompt
    assert "- amount" in prompt
    assert "Top merchants?" in prompt
    assert "Return SQL only" in prompt


def test_answer_with_sql_executes_safe_generated_query() -> None:
    stored_table = _stored_table()
    llm = FakeLLM(
        text=(
            "SELECT merchant, SUM(amount) AS total_amount "
            "FROM uploaded_data GROUP BY merchant "
            "ORDER BY total_amount DESC, merchant ASC"
        )
    )

    result = answer_with_sql("Which merchants have the most amount?", stored_table, llm)

    assert result.sql.startswith("SELECT merchant")
    assert result.result["merchant"].tolist() == ["A", "B"]
    assert result.result["total_amount"].tolist() == [30.0, 30.0]
    assert "Which merchants" in llm.last_prompt


def test_answer_with_sql_rejects_unsafe_generated_query() -> None:
    stored_table = _stored_table()
    llm = FakeLLM(text="DROP TABLE uploaded_data")

    with pytest.raises(UnsafeQueryError):
        answer_with_sql("Delete the table", stored_table, llm)
