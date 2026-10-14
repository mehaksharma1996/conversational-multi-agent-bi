"""Run every workflow case through the real API with the scripted model and check the baseline.

DEVELOPMENT AND TEST ONLY. It depends on ``tests.e2e.retail_app`` (the scripted model), so it lives
under ``tests/`` and is never imported by ``scripts/``, ``packages/``, ``src/``, or ``apps/``.
The pytest suite runs it as the CI check (``tests/test_workflow_baseline.py``); people run it as:

    python -m tests.e2e.workflow_baseline_runner                       # compare; exit 1 on drift
    python -m tests.e2e.workflow_baseline_runner --update --justification "why this is right"

Start it from a working directory that has no ``.env`` with ``PYTHON_DOTENV_DISABLED=1`` and the
repository on ``PYTHONPATH``: ChromaDB reads ``.env`` from the working directory on its own, so
disabling python-dotenv alone is not enough. The command refuses to run otherwise.

The harness adds instrumentation around the unchanged application; it changes no product code:

- every SQLite connection the query executor opens gets a trace callback, so every statement SQLite
  actually ran is checked, not only the SQL shown in the answer;
- an ASGI recorder keeps each response's status and body (in memory only, for the leakage check);
- dataset databases are digested through read-only connections before the first question and after
  the last;
- a process audit hook records any socket connection or name lookup to a non-loopback host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "evals" / "baseline" / "v2" / "manifest.json"
BASELINE_PATH = ROOT / "evals" / "baseline" / "v2" / "workflow-baseline.json"
PROMPT_REGISTRY = ROOT / "evals" / "v1" / "prompt-registry.json"
SCRIPTED_SECRET = "scripted-not-a-credential"
MODE = "scripted-baseline"


# --------------------------------------------------------------------------------------------
# Network audit hook (installed once per process; inert unless a run is recording)
# --------------------------------------------------------------------------------------------

_network_lock = threading.Lock()
_network_attempts: list[str] | None = None
_hook_installed = False


def _audit(event: str, args: tuple[Any, ...]) -> None:
    attempts = _network_attempts
    if attempts is None:
        return
    host: Any = None
    if event == "socket.connect" and len(args) >= 2:
        address = args[1]
        host = address[0] if isinstance(address, tuple) and address else "localhost"
    elif event == "socket.getaddrinfo" and args:
        host = args[0]
    if host is None:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    attempts.append(str(host))


@contextmanager
def _record_network() -> Iterator[list[str]]:
    global _network_attempts, _hook_installed
    with _network_lock:
        if not _hook_installed:
            sys.addaudithook(_audit)
            _hook_installed = True
        attempts: list[str] = []
        _network_attempts = attempts
    try:
        yield attempts
    finally:
        _network_attempts = None


# --------------------------------------------------------------------------------------------
# SQL trace on the query executor's connections
# --------------------------------------------------------------------------------------------


class _Trace:
    def __init__(self) -> None:
        from packages.evaluation.workflow_baseline import TracedStatement

        self._statement = TracedStatement
        self.statements: list[Any] = []
        self.current_request: int | None = None

    @contextmanager
    def installed(self) -> Iterator[None]:
        from src.storage import encrypted_sqlite, query_executor

        def traced_connect(database: Any, *, encryption_key: Any, uri: bool = False) -> Any:
            connection = encrypted_sqlite.connect(database, encryption_key=encryption_key, uri=uri)
            text = str(database)
            path, _, query = text.removeprefix("file:").partition("?") if uri else (text, "", "")
            read_only = uri and parse_qs(query).get("mode") == ["ro"]
            resolved = str(Path(path).resolve())
            request = self.current_request

            def record(sql: str) -> None:
                self.statements.append(self._statement(request, resolved, read_only, sql))

            connection.set_trace_callback(record)
            return connection

        shim = SimpleNamespace(connect=traced_connect)
        with mock.patch.object(query_executor, "encrypted_sqlite", shim):
            yield


# --------------------------------------------------------------------------------------------
# Response recorder and dataset snapshots
# --------------------------------------------------------------------------------------------


def _dataset_paths(workdir: Path) -> list[Path]:
    return sorted(workdir.rglob("sqlite/app.db"))


class _Recorder:
    def __init__(self, trace: _Trace, workdir: Path) -> None:
        self.trace = trace
        self.workdir = workdir
        self.exchanges: list[Any] = []
        self.before: Any = None

    def begin(self, method: str, path: str) -> int:
        from packages.evaluation.workflow_baseline import snapshot_datasets

        index = len(self.exchanges)
        self.exchanges.append(None)
        if method == "POST" and path.endswith("/messages") and self.before is None:
            # The questions start now: everything before this was setup (upload, indexing).
            self.before = snapshot_datasets(_dataset_paths(self.workdir))
        self.trace.current_request = index
        return index

    def end(self, index: int, method: str, path: str, status: int | None, body: bytes) -> None:
        from packages.evaluation.workflow_baseline import HttpExchange

        self.trace.current_request = None
        self.exchanges[index] = HttpExchange(index, method, path, status, body)

    def middleware(self) -> type:
        recorder = self

        class Middleware:
            def __init__(self, app: Any) -> None:
                self.app = app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                method, path = scope["method"], scope["path"]
                index = recorder.begin(method, path)
                state: dict[str, Any] = {"status": None, "body": []}

                async def capture(message: Any) -> None:
                    if message["type"] == "http.response.start":
                        state["status"] = message["status"]
                    elif message["type"] == "http.response.body":
                        state["body"].append(message.get("body", b""))
                    await send(message)

                try:
                    await self.app(scope, receive, capture)
                finally:
                    recorder.end(index, method, path, state["status"], b"".join(state["body"]))

        return Middleware


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


def run_baseline(
    workdir: Path,
    *,
    repo_root: Path = ROOT,
    llm: Any | None = None,
    case_ids: list[str] | None = None,
    tamper: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """One scripted run of every manifest case; returns the current baseline document.

    ``llm`` and ``tamper`` exist for tests: a different scripted model, or a hook that alters the
    collected evidence before it is judged (to prove the invariants fail closed).
    """
    from config.settings import Settings
    from packages.evaluation.live_demo import LiveBudget
    from packages.evaluation.workflow_baseline import (
        BaselineError,
        ModelEvidence,
        RunEvidence,
        build_baseline,
        combine_case_documents,
        fixture_hashes,
        snapshot_datasets,
    )
    from packages.evaluation.workflow_cases import fixture_sha256, load_json, validate_manifest
    from scripts.live_demo_runner import RunInputs, run_live_demo_eval
    from tests.e2e.retail_app import MODEL, PROVIDER, ScriptedRetailEmbedder, ScriptedRetailLLM

    manifest = load_json(MANIFEST_PATH)
    problems = validate_manifest(manifest, repo_root)
    if problems:
        raise BaselineError("invalid manifest: " + "; ".join(problems))
    sources = {
        name: load_json(repo_root / entry["path"]) for name, entry in manifest["sources"].items()
    }
    combined = combine_case_documents(list(sources.values()))
    definitions = {case["id"]: case for case in combined["cases"]}
    cases_path = workdir / "workflow-cases.json"
    cases_path.write_text(json.dumps(combined), encoding="utf-8")

    scripted = llm if llm is not None else ScriptedRetailLLM()
    calls = {"n": 0}

    class Counting:
        provider, model, configured = scripted.provider, scripted.model, True

        def generate(self, prompt: str) -> Any:
            calls["n"] += 1
            return scripted.generate(prompt)

    data_dir = workdir / "app"
    settings = Settings(
        app_data_dir=data_dir,
        sqlite_db_path=data_dir / "sqlite" / "app.db",
        chroma_persist_dir=data_dir / "vectorstore",
        gemini_api_key=SCRIPTED_SECRET,
        gemini_model=MODEL,
        embedding_model="deterministic-scripted",
        retrieval_max_distance=None,
    )
    budget = LiveBudget(
        max_calls=500,
        max_output_tokens=2048,
        max_total_tokens=50_000_000,
        max_retries=0,
        timeout_seconds=60.0,
        max_run_seconds=1800.0,
    )
    trace = _Trace()
    recorder = _Recorder(trace, data_dir)

    def app_factory(**kwargs: Any) -> Any:
        from apps.api.main import create_app

        app = create_app(**kwargs)
        app.add_middleware(recorder.middleware())
        return app

    fixtures_before = fixture_hashes(manifest, repo_root)
    with _record_network() as network, trace.installed():
        report = run_live_demo_eval(
            RunInputs(
                cases_path=cases_path,
                repo_root=repo_root,
                workdir=data_dir,
                settings=settings,
                budget=budget,
                mode=MODE,
                secrets=[SCRIPTED_SECRET],
                embedder_factory=lambda _model: ScriptedRetailEmbedder(),
                case_ids=case_ids,
                clock=lambda: "not-recorded",
            ),
            Counting(),
            app_factory=app_factory,
        )
        network_attempts = list(network)
    dataset_after = snapshot_datasets(_dataset_paths(data_dir))
    fixtures_after = fixture_hashes(manifest, repo_root)

    exchanges = list(recorder.exchanges)
    messages: dict[str, Any] = {}
    for exchange, record in zip(
        [e for e in exchanges if e is not None and e.is_message], report["cases"], strict=False
    ):
        try:
            messages[record["id"]] = json.loads(exchange.body)
        except ValueError:
            continue
    evidence = RunEvidence(
        report=report,
        exchanges=None if any(e is None for e in exchanges) else exchanges,
        statements=list(trace.statements),
        dataset_before=recorder.before,
        dataset_after=dataset_after,
        fixtures_before=fixtures_before,
        fixtures_after=fixtures_after,
        model=ModelEvidence(PROVIDER, MODEL, calls["n"], network_attempts),
        secrets=[SCRIPTED_SECRET],
        forbidden_fragments=sorted(
            {f for case in combined["cases"] for f in case.get("forbidden_fragments", [])}
        ),
        messages=messages,
    )
    if tamper is not None:
        tamper(evidence)
    if case_ids is not None:
        # A partial run leaves the other cases unasked: report them as not attempted.
        skipped = [c for c in definitions if c not in {r["id"] for r in report["cases"]}]
        report["summary"]["cases_not_attempted"] = sorted(
            set(report["summary"]["cases_not_attempted"]) | set(skipped)
        )
    return build_baseline(
        manifest,
        fixture_sha256(MANIFEST_PATH, "sha256-lf"),
        definitions,
        evidence,
        extra_provenance={
            "cases_version": combined["version"],
            "prompt_registry_sha256": (
                fixture_sha256(PROMPT_REGISTRY, "sha256-lf") if PROMPT_REGISTRY.exists() else None
            ),
        },
    )


# --------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------


def isolation_problems() -> list[str]:
    problems = []
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        problems.append("set PYTHON_DOTENV_DISABLED=1")
    cwd = Path.cwd().resolve()
    if (cwd / ".env").exists():
        problems.append("start from a working directory that has no .env (ChromaDB reads it)")
    if cwd == ROOT.resolve():
        problems.append("do not start from the repository root")
    return problems


def _print_summary(baseline: dict[str, Any]) -> None:
    metrics = baseline["metrics"]
    overall = metrics["overall"]
    print(f"[{baseline['evidence']['kind']}] {baseline['evidence']['statement']}")
    print(
        f"  cases {metrics['cases']}: measured {metrics['measured']}, passed {metrics['passed']}, "
        f"failed {metrics['failed']}, not attempted {metrics['not_attempted']}, "
        f"not measured {metrics['not_measured']}"
    )
    print(f"  overall pass rate {overall['pass_rate']} (Wilson 95% {overall['wilson_95']})")
    for name, verdict in baseline["safety"].items():
        print(f"  safety {name:<24} {verdict['status']} ({verdict['reason']})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted workflow baseline (development only).")
    parser.add_argument("--update", action="store_true", help="Write the baseline if allowed.")
    parser.add_argument("--justification", default="", help="Required for any accepted change.")
    parser.add_argument("--output", type=Path, help="Also write the current baseline here.")
    args = parser.parse_args(argv)

    problems = isolation_problems()
    if problems:
        print("Refusing to run: " + "; ".join(problems), file=sys.stderr)
        return 2

    from packages.evaluation.workflow_baseline import (
        compare_baselines,
        decide_update,
        render,
        safety_failures,
        with_review,
    )

    with tempfile.TemporaryDirectory(
        prefix="workflow-baseline-", ignore_cleanup_errors=True
    ) as tmp:
        current = run_baseline(Path(tmp))
    _print_summary(current)
    if args.output:
        args.output.write_text(render(current), encoding="utf-8", newline="\n")

    committed = (
        json.loads(BASELINE_PATH.read_text(encoding="utf-8")) if BASELINE_PATH.exists() else None
    )
    if args.update:
        decision = decide_update(committed, current, args.justification)
        if decision.comparison is not None:
            _print_comparison(decision.comparison)
        if not decision.allowed:
            for reason in decision.reasons:
                print(f"UPDATE REFUSED: {reason}", file=sys.stderr)
            return 1
        BASELINE_PATH.write_text(
            render(with_review(current, args.justification, decision.comparison)),
            encoding="utf-8",
            newline="\n",
        )
        print(f"Baseline written to {BASELINE_PATH}; review the diff before committing.")
        return 0

    failures = safety_failures(current)
    if committed is None:
        print("No committed baseline. Create one with --update --justification.", file=sys.stderr)
        return 1
    comparison = compare_baselines(committed, current)
    _print_comparison(comparison)
    if failures:
        print(f"SAFETY FAILED: {', '.join(failures)}", file=sys.stderr)
    return 0 if comparison.identical and not failures else 1


def _print_comparison(comparison: Any) -> None:
    if comparison.identical:
        print("Matches the committed baseline.")
        return
    for title, changes in (
        ("CRITICAL", comparison.critical),
        ("NONCRITICAL", comparison.noncritical),
    ):
        for change in changes:
            print(f"  {title} {change.describe()}")


if __name__ == "__main__":
    raise SystemExit(main())
