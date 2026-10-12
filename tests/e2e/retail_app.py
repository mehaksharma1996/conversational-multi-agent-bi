"""Scripted-provider API for the retail demo browser journeys. NOT a live model.

This module replaces the language model with a deterministic script: it recognises the nine sample
questions and returns the route, the policy predicates, the SQL, and the quoted document sentence a
well-behaved model would. Everything else (retrieval, the SQL guard, execution, insight rendering,
citation and policy checks, approval, persistence, authorization) is the real production code.

What a run against this app proves: the application and UI handle correct model behaviour.
What it cannot prove: that any real model produces these outputs. Never cite it as live-model
validation. It is mounted only by tests (``scripts/run_e2e_stack.py`` starts it with
``uvicorn --factory``) and is not part of any image or default configuration.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import FastAPI

from apps.api.main import create_app
from config.settings import Settings
from src.llm.base import LLMGenerationError, LLMResponse

PROVIDER = "scripted-retail-demo"
MODEL = "scripted-retail-demo-v1"

REGION = "Which region has the highest total revenue?"
TOTAL = "What is total revenue across the dataset?"
CATEGORY = "Which category generated the most gross profit?"
MONTH = "Which month had the highest revenue?"
H1 = "How did first-half revenue change from 2025 to 2026?"
SEGMENT = "How many transactions have an unknown customer segment?"
RETURN_RULE = "Does a return by itself require escalation?"
FULFILLMENT = "How many transactions meet the policy fulfillment escalation rule?"
REVIEW = "How many transactions meet at least one documented review rule?"

HYBRID_QUESTIONS = (FULFILLMENT, REVIEW)

# Not sample questions: they exist so browser tests can exercise a large result, an empty result,
# and a provider failure. They are never offered in the UI.
LARGE = "Show every transaction."
EMPTY = "Show transactions in the Moon region."
FAIL = "Trigger a provider failure."

# Table is queried as "uploaded_data"; column names are those of retail_operations.csv.
SQL_BY_QUESTION: dict[str, str] = {
    TOTAL: 'SELECT ROUND(SUM("revenue"), 2) AS "total_revenue" FROM "uploaded_data"',
    REGION: (
        'SELECT "region", ROUND(SUM("revenue"), 2) AS "total_revenue" FROM "uploaded_data" '
        'GROUP BY "region" ORDER BY "total_revenue" DESC, "region" ASC'
    ),
    CATEGORY: (
        'SELECT "category", ROUND(SUM("gross_profit"), 2) AS "gross_profit" '
        'FROM "uploaded_data" GROUP BY "category" ORDER BY "gross_profit" DESC, "category" ASC'
    ),
    MONTH: (
        'SELECT substr("transaction_date", 1, 7) AS "month", '
        'ROUND(SUM("revenue"), 2) AS "total_revenue" FROM "uploaded_data" '
        'GROUP BY substr("transaction_date", 1, 7) ORDER BY "total_revenue" DESC, "month" ASC'
    ),
    H1: (
        'SELECT substr("transaction_date", 1, 4) AS "year", '
        'ROUND(SUM("revenue"), 2) AS "h1_revenue" FROM "uploaded_data" '
        "WHERE substr(\"transaction_date\", 6, 2) <= '06' "
        "AND substr(\"transaction_date\", 1, 4) IN ('2025', '2026') "
        'GROUP BY substr("transaction_date", 1, 4) ORDER BY "year" ASC'
    ),
    SEGMENT: (
        'SELECT COUNT(*) AS "missing_customer_segment_count" FROM "uploaded_data" '
        'WHERE "customer_segment" IS NULL'
    ),
    FULFILLMENT: (
        'SELECT COUNT(*) AS "transaction_count" FROM "uploaded_data" WHERE "fulfillment_days" >= 10'
    ),
    REVIEW: (
        'SELECT COUNT(*) AS "review_transaction_count" FROM "uploaded_data" '
        'WHERE "discount_rate" >= 0.30 OR "gross_margin_rate" < 0.10 OR "fulfillment_days" >= 10'
    ),
    LARGE: (
        'SELECT "transaction_id", "region", "category", "revenue" FROM "uploaded_data" '
        'ORDER BY "transaction_id"'
    ),
    EMPTY: ('SELECT "transaction_id", "revenue" FROM "uploaded_data" WHERE "region" = \'Moon\''),
}

# field, operator, value, unit, a phrase that identifies the policy sentence it comes from.
PREDICATES: dict[str, list[tuple[str, str, float | int, str, str]]] = {
    FULFILLMENT: [("fulfillment_days", "gte", 10, "days", "fulfillment_days of 10")],
    REVIEW: [
        ("discount_rate", "gte", 0.30, "decimal_fraction", "discount_rate of 0.30"),
        ("gross_margin_rate", "lt", 0.10, "decimal_fraction", "gross_margin_rate below 0.10"),
        ("fulfillment_days", "gte", 10, "days", "fulfillment_days of 10"),
    ],
}

QUOTE_BY_QUESTION = {
    RETURN_RULE: "A returned transaction does not by itself require escalation.",
    FULFILLMENT: "fulfillment_days of 10 or greater requires escalation",
    REVIEW: "discount_rate of 0.30 or greater requires review",
}


_QUESTION_LINE = re.compile(r"^Question:[ \t]*\n?([^\n]+)", re.MULTILINE)


def _current_question(prompt: str) -> str | None:
    """The question being answered, not earlier ones quoted as conversation context."""
    matches = _QUESTION_LINE.findall(prompt)
    return matches[-1].strip() if matches else None


def _question_in(prompt: str) -> str | None:
    current = _current_question(prompt)
    known = SQL_BY_QUESTION | QUOTE_BY_QUESTION
    return current if current in known else None


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class ScriptedRetailEmbedder:
    """Constant vectors: every chunk of the short demo policy is retrieved for any question."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.5, 0.25] for _ in texts]


class ScriptedRetailLLM:
    provider = PROVIDER
    model = MODEL
    configured = True

    def generate(self, prompt: str) -> LLMResponse:
        question = _question_in(prompt)
        if _current_question(prompt) == FAIL:
            raise LLMGenerationError("The scripted provider failed on purpose.")
        if "Classify a business-intelligence question" in prompt:
            if question in HYBRID_QUESTIONS:
                route = "hybrid"
            elif question == RETURN_RULE:
                route = "rag"
            else:
                route = "sql"
            text = json.dumps({"route": route, "confidence": 1})
        elif "Extract business criteria" in prompt:
            text = json.dumps(self._criteria(prompt, question))
        elif "careful document analyst" in prompt:
            text = self._document_answer(prompt, question)
        else:
            text = SQL_BY_QUESTION.get(
                question or "", 'SELECT COUNT(*) AS "row_count" FROM "uploaded_data"'
            )
        return LLMResponse(text=text, model=self.model, provider=self.provider)

    @staticmethod
    def _criteria(prompt: str, question: str | None) -> dict[str, object]:
        excerpts_json = prompt.split("Excerpts: ", 1)[1].splitlines()[0]
        excerpts = [_squash(item).lower() for item in json.loads(excerpts_json)]
        predicates = []
        for field, operator, value, unit, phrase in PREDICATES.get(question or "", []):
            source = next(
                (index for index, text in enumerate(excerpts, start=1) if phrase in text), None
            )
            if source is not None:
                predicates.append(
                    {
                        "field": field,
                        "operator": operator,
                        "value": value,
                        "unit": unit,
                        "source": source,
                        "null_behavior": "exclude",
                    }
                )
        if not predicates:
            return {"keywords": []}
        return {
            "thresholds": [predicate["value"] for predicate in predicates],
            "predicates": predicates,
            "predicate_join": "any" if len(predicates) > 1 else "all",
        }

    @staticmethod
    def _document_answer(prompt: str, question: str | None) -> str:
        quote = QUOTE_BY_QUESTION.get(question or "")
        blocks = re.findall(r"\[Source (\d+): [^\]]*\]\n(.*?)(?=\n\n\[Source|\Z)", prompt, re.S)
        for number, text in blocks:
            if quote and quote in _squash(text):
                return f'The policy states "{quote}" Source {number}'
        return "The uploaded document does not provide enough information."


def build_app() -> FastAPI:
    root = Path(os.environ.get("E2E_DATA_DIR", "work/e2e-data")).resolve()
    settings = Settings(
        app_data_dir=root,
        sqlite_db_path=root / "sqlite" / "app.db",
        chroma_persist_dir=root / "vectorstore",
        gemini_api_key="scripted-not-a-credential",
        gemini_model=MODEL,
        embedding_model="deterministic-scripted",
        api_auth_mode="local",
        local_only_mode=False,
        durable_metadata=True,
        retrieval_max_distance=None,
        rate_limit_messages=1_000,
        rate_limit_tabular_uploads=1_000,
        rate_limit_document_indexes=1_000,
        rate_limit_analyses=1_000,
    )
    return create_app(
        settings=settings,
        embedder_factory=lambda _model: ScriptedRetailEmbedder(),
        llm_client_factory=lambda _settings: ScriptedRetailLLM(),
    )
