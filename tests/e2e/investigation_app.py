"""Scripted investigation APIs for the UI-6b (#155) browser journeys. NOT a live model.

``build_scripted_app`` enables investigation routing and injects the same bounded, test-only
planning and execution adapters as ``tests/test_api_investigations.py``; everything else (routing,
planning admission, approval, the SQL gateway, jobs, cancellation, persistence, restart
reconciliation) is the real production code. ``build_production_app`` enables routing with no
injected adapters, exactly as production is configured until PROD-1 (#198), so planning must fail
closed as ``metering_unbounded``.

What a run proves: the browser and API wiring of the investigation workflow. What it cannot prove:
that any production model is supported. Never cite it as live-model validation. It is mounted only
by ``apps/web/e2e/real/stack.ts`` and is not part of any image or default configuration.

The ordinary model client records every call as one content-free line in ``model-calls.log`` in
the data directory, so a journey can prove that no model was called.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from apps.api.investigations import ExecutionProvider, PlanningProvider
from apps.api.main import create_app
from config.settings import Settings
from src.llm.base import LLMGenerationError, LLMResponse
from tests.e2e.retail_app import ScriptedRetailEmbedder
from tests.investigation_execution_fakes import RoutedAdapter
from tests.investigation_fixtures import plan_of, sql_task
from tests.investigation_planning_fakes import ENVELOPE, ScriptedDeadlineAdapter, evidence, pricing
from tests.test_investigation_sql_gateway import GOOD_SQL
from tests.test_investigation_tool_gateway import EXACT_BOUND, EXACT_ENVELOPE, MAX_INPUT

MODEL_CALL_LOG = "model-calls.log"


def _data_root() -> Path:
    return Path(os.environ.get("E2E_DATA_DIR", "work/e2e-investigation-data")).resolve()


@dataclass(frozen=True)
class _ScriptedRecipientSettings(Settings):
    """Consent names the scripted execution adapter, as a reviewed provider would be named."""

    def hosted_recipients(self) -> tuple[str, ...]:
        recipients = super().hosted_recipients()
        extra = EXACT_BOUND.data_recipient
        return recipients if extra is None or extra in recipients else (*recipients, extra)


class RecordingLLM:
    """The ordinary question model. No investigation journey should reach it."""

    provider = "scripted-investigation-e2e"
    model = "scripted-investigation-e2e-v1"
    configured = True

    def __init__(self, root: Path) -> None:
        self._log = root / MODEL_CALL_LOG

    def generate(self, prompt: str) -> LLMResponse:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write("call\n")  # content-free: the prompt is never written
        raise LLMGenerationError("The investigation journeys never call the ordinary model.")


class ScriptedInvestigationAdapters:
    """One SQL task owned by the SQL analyst; its SQL pauses for a statement-level decision."""

    def __init__(self) -> None:
        self.plan = plan_of(sql_task("trend", fields=("quarter", "churned")))

    def planning(self) -> PlanningProvider:
        text = json.dumps({"plan": self.plan.model_dump(mode="json"), "missing_information": []})
        return PlanningProvider(
            adapter=ScriptedDeadlineAdapter(answers=[text]),
            evidence=evidence(),
            pricing=pricing(),
            envelope=ENVELOPE,
        )

    def execution(self) -> ExecutionProvider:
        # A new adapter per run, so every journey's statement is the first scripted answer.
        return ExecutionProvider(
            llm_client=RoutedAdapter(script=[GOOD_SQL]),
            envelope=EXACT_ENVELOPE,
            max_input_tokens_per_call=MAX_INPUT,
        )


def _settings(root: Path, settings_type: type[Settings]) -> Settings:
    return settings_type(
        app_data_dir=root,
        sqlite_db_path=root / "sqlite" / "app.db",
        chroma_persist_dir=root / "vectorstore",
        gemini_api_key="scripted-not-a-credential",
        gemini_model=RecordingLLM.model,
        embedding_model="deterministic-scripted",
        api_auth_mode="local",
        local_only_mode=False,
        durable_metadata=True,
        investigation_routing_enabled=True,
        rate_limit_messages=1_000,
        rate_limit_tabular_uploads=1_000,
        rate_limit_document_indexes=1_000,
        rate_limit_analyses=1_000,
    )


def build_scripted_app() -> FastAPI:
    root = _data_root()
    return create_app(
        settings=_settings(root, _ScriptedRecipientSettings),
        embedder_factory=lambda _model: ScriptedRetailEmbedder(),
        llm_client_factory=lambda _settings: RecordingLLM(root),
        investigation_adapters=ScriptedInvestigationAdapters(),
    )


def build_production_app() -> FastAPI:
    root = _data_root()
    return create_app(
        settings=_settings(root, Settings),
        embedder_factory=lambda _model: ScriptedRetailEmbedder(),
        llm_client_factory=lambda _settings: RecordingLLM(root),
    )
