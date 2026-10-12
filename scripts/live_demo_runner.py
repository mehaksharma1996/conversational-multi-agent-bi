"""Drive the real API through the retail demo with an injected model and score every answer.

The scoring, budget, and oracle live in ``packages/evaluation/live_demo.py`` (framework-free);
this module owns the FastAPI side: building the app, uploading the demo files, and asking.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.main import create_app
from config.settings import Settings
from packages.evaluation.live_demo import (
    CONSECUTIVE_FAILURE_LIMIT,
    LIMITATIONS,
    REPORT_SCHEMA,
    BudgetedLLM,
    LiveBudget,
    evaluate_message,
    excerpt,
    load_cases,
    load_oracle,
    optional_sha,
    sha256_file,
    summarise,
)

# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


@dataclass
class RunInputs:
    cases_path: Path
    repo_root: Path
    workdir: Path
    settings: Settings
    budget: LiveBudget
    mode: str  # "live" or "scripted-selftest"; the report never blurs the two
    secrets: Sequence[str] = ()
    embedder_factory: Callable[[str], Any] | None = None
    case_ids: Sequence[str] | None = None
    clock: Callable[[], str] = field(default=lambda: datetime.now(UTC).isoformat())


def build_run_settings(base: Settings, workdir: Path, budget: LiveBudget) -> Settings:
    """Isolated storage, local auth, and rate limits that do not interfere with the budget."""
    return replace(
        base,
        app_data_dir=workdir,
        sqlite_db_path=workdir / "sqlite" / "app.db",
        chroma_persist_dir=workdir / "vectorstore",
        api_auth_mode="local",
        durable_metadata=False,
        rate_limit_messages=10_000,
        rate_limit_tabular_uploads=1_000,
        rate_limit_document_indexes=1_000,
        rate_limit_analyses=1_000,
    )


class _Workspace(NamedTuple):
    id: str
    dataset_id: str
    document_collection_id: str


def _open_workspace(client: TestClient, root: Path, truth_files: Mapping[str, str]) -> _Workspace:
    workspace = client.post("/api/v1/workspaces").json()["id"]
    csv_path = root / truth_files["dataset"]
    upload = client.post(
        f"/api/v1/workspaces/{workspace}/tabular-uploads",
        files={"file": (csv_path.name, csv_path.read_bytes())},
    )
    upload.raise_for_status()
    dataset = client.post(f"/api/v1/tabular-uploads/{upload.json()['id']}/dataset", json={})
    dataset.raise_for_status()
    body = dataset.json()
    mapping = {f: item["source_column"] for f, item in body["schema_mapping"]["mappings"].items()}
    client.put(f"/api/v1/datasets/{body['id']}/schema-mapping", json=mapping).raise_for_status()
    policy = root / truth_files["policy"]
    documents = client.post(
        f"/api/v1/workspaces/{workspace}/document-collections",
        files=[("files", (policy.name, policy.read_bytes(), "application/pdf"))],
    )
    documents.raise_for_status()
    client.put(
        f"/api/v1/workspaces/{workspace}/consent",
        json={"accepted": True, "notice_version": "2026-09"},
    ).raise_for_status()
    return _Workspace(str(workspace), str(body["id"]), str(documents.json()["id"]))


def _new_conversation(client: TestClient, workspace: _Workspace) -> str:
    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/conversations",
        json={
            "dataset_id": workspace.dataset_id,
            "document_collection_id": workspace.document_collection_id,
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def run_live_demo_eval(
    inputs: RunInputs, inner_llm: Any, app_factory: Callable[..., FastAPI] = create_app
) -> dict[str, Any]:
    document = load_cases(inputs.cases_path)
    root = inputs.repo_root
    truth_files = {"dataset": document["dataset"], "policy": document["policy"]}
    oracle = load_oracle(root / document["dataset"], document["table"])
    cases = [c for c in document["cases"] if inputs.case_ids is None or c["id"] in inputs.case_ids]

    budgeted = BudgetedLLM(inner_llm, inputs.budget)
    settings = build_run_settings(inputs.settings, inputs.workdir, inputs.budget)
    app = app_factory(
        settings=settings,
        embedder_factory=inputs.embedder_factory,
        llm_client_factory=lambda _settings: budgeted,
    )

    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    stop_reason: str | None = None
    consecutive_failures = 0
    conversations: dict[str, str] = {}

    with TestClient(app) as client:
        workspace = _open_workspace(client, root, truth_files)
        for case in cases:
            if budgeted.stop_reason:
                stop_reason = budgeted.stop_reason
                break
            if time.perf_counter() - started > inputs.budget.max_run_seconds:
                stop_reason = "max_run_seconds"
                break
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                stop_reason = "consecutive_provider_failures"
                break
            follows = case.get("follows")
            conversation = (
                conversations[follows]
                if follows and follows in conversations
                else _new_conversation(client, workspace)
            )
            conversations[case["id"]] = conversation

            before = (
                budgeted.calls,
                budgeted.prompt_tokens,
                budgeted.output_tokens,
                budgeted.failures,
                sum(r.duration_ms for r in budgeted.records),
            )
            t0 = time.perf_counter()
            response = client.post(
                f"/api/v1/conversations/{conversation}/messages",
                json={"question": case["question"]},
            )
            wall_ms = (time.perf_counter() - t0) * 1000
            calls = budgeted.calls - before[0]
            failures = budgeted.failures - before[3]
            call_records = budgeted.records[before[0] :]
            consecutive_failures = consecutive_failures + 1 if failures and calls == failures else 0

            record: dict[str, Any] = {
                "id": case["id"],
                "kind": case["kind"],
                "scored": case["scored"],
                "question": case["question"],
                "http_status": response.status_code,
                "model_calls": calls,
                "provider_failures": failures,
                "prompt_tokens": budgeted.prompt_tokens - before[1],
                "output_tokens": budgeted.output_tokens - before[2],
                "model_latency_ms": round(
                    sum(r.duration_ms for r in budgeted.records) - before[4], 1
                ),
                "call_diagnostics": [
                    {
                        "purpose": item.purpose,
                        "outcome": item.outcome,
                        "prompt_tokens": item.prompt_tokens,
                        "output_tokens": item.output_tokens,
                        "usage_reported": item.usage_reported,
                        "near_output_limit": (
                            item.output_tokens is not None
                            and item.output_tokens >= inputs.budget.max_output_tokens * 0.9
                        ),
                    }
                    for item in call_records
                ],
                "wall_ms": round(wall_ms, 1),
            }
            if response.status_code != 201:
                record.update(passed=False, checks={}, error=f"http_{response.status_code}")
            else:
                message = response.json()
                checks = evaluate_message(case, message, oracle, inputs.secrets)
                applicable = [v for v in checks.values() if v is not None]
                record.update(
                    route=message["route"],
                    status=message["status"],
                    sql=message["sql"],
                    row_count=None if message["rows"] is None else len(message["rows"]),
                    answer_excerpt=excerpt(str(message["answer"])),
                    grounding_status=message["provenance"]["grounding_status"],
                    policy_sql_status=message["provenance"]["policy_sql_status"],
                    structured_output_repairs=message["provenance"]["execution"][
                        "structured_output_repairs"
                    ],
                    structured_output_failures=message["provenance"]["execution"][
                        "structured_output_failures"
                    ],
                    checks=checks,
                    passed=(all(applicable) if case["scored"] and applicable else None),
                )
            results.append(record)

    attempted_ids = {r["id"] for r in results}
    summary = summarise(results, [c for c in cases if c["id"] not in attempted_ids], budgeted)
    summary["stop_reason"] = stop_reason or budgeted.stop_reason or "completed"
    return {
        "schema": REPORT_SCHEMA,
        "metadata": {
            "mode": inputs.mode,
            "model_is_live": inputs.mode == "live",
            "provider": budgeted.provider,
            "model": budgeted.model,
            "date_utc": inputs.clock(),
            "cases_version": document["version"],
            "cases_sha256": sha256_file(inputs.cases_path),
            "dataset_sha256": sha256_file(root / document["dataset"]),
            "policy_sha256": sha256_file(root / document["policy"]),
            "prompt_registry_sha256": optional_sha(root / "evals" / "v1" / "prompt-registry.json"),
            "budget": inputs.budget.describe(),
            "embedding_model": inputs.settings.embedding_model,
        },
        "summary": summary,
        "cases": results,
        "limitations": LIMITATIONS,
    }
