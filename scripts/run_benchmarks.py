"""Repeatable, dependency-free benchmarks for the deterministic core (issue #31b).

Measures the analysis pipeline, SQL validation and execution, hybrid retrieval, and PDF indexing on
seeded synthetic data. Nothing is downloaded and no model or network is used: embeddings come from
the offline hashing embedder, so results isolate this repository's code, not a provider.

    python -m scripts.run_benchmarks                         # standard sizes, table to stdout
    python -m scripts.run_benchmarks --output bench.json     # also write machine-readable results
    python -m scripts.run_benchmarks --quick                 # tiny sizes (smoke test / CI sanity)
    python -m scripts.run_benchmarks --only sql_execute      # one or more benchmarks by name
    python -m scripts.run_benchmarks --profile pdf_index/pages=60   # cProfile one benchmark
    python -m scripts.run_benchmarks --list

Timings are indicative, machine-dependent baselines. This script never fails on a slow result: it
exits non-zero only when a benchmark cannot run, so it cannot make the quality gate flaky. Results
carry timings and sizes only, never generated text, rows, or queries.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import platform
import pstats
import random
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
from reportlab.pdfgen import canvas

from packages.evaluation.fakes import HashingEmbedder
from packages.retrieval.service import (
    DocumentApplicationService,
    DocumentPayload,
    IndexDocumentsCommand,
)
from src.analytics.anomaly_detection import recommend_anomaly_features
from src.analytics.pipeline import build_analysis_bundle
from src.documents.chunker import chunk_document_pages
from src.documents.retriever import DocumentRetriever
from src.documents.vector_store import ChromaDocumentStore
from src.ingestion.pdf_loader import load_pdf_file
from src.profiling.data_profiler import profile_dataframe
from src.profiling.schema_mapper import map_schema
from src.storage.query_executor import execute_read_query, validate_read_query
from src.storage.sqlite_store import DEFAULT_TABLE_NAME, SQLiteStore

SCHEMA_VERSION = 1
SEED = 20261006
REPO_ROOT = Path(__file__).resolve().parents[1]

_REGIONS = ("East", "West", "North", "South", "Central")
_STATUSES = ("approved", "approved", "approved", "declined", "pending")
_VOCABULARY = (
    "policy review approval manager threshold refund invoice vendor payment audit control "
    "exception escalation quarterly budget forecast revenue expense ledger contract renewal "
    "compliance report evidence sample region customer account transaction limit risk finance "
    "procurement supplier delivery inventory warehouse shipment tax discount balance statement"
).split()

SQL_QUERIES = {
    "filter_limit": "SELECT * FROM uploaded_data WHERE amount > 500 ORDER BY amount DESC",
    "group_aggregate": (
        "SELECT region, status, COUNT(*) AS n, SUM(amount) AS total, AVG(amount) AS mean "
        "FROM uploaded_data GROUP BY region, status ORDER BY total DESC"
    ),
    "window_cte": (
        "WITH ranked AS (SELECT merchant, amount, "
        "RANK() OVER (PARTITION BY region ORDER BY amount DESC) AS r FROM uploaded_data) "
        "SELECT merchant, amount FROM ranked WHERE r <= 5"
    ),
}


@dataclass(frozen=True)
class Sizes:
    analysis_rows: tuple[int, ...]
    sql_rows: tuple[int, ...]
    retrieval_chunks: tuple[int, ...]
    pdf_pages: tuple[int, ...]
    repeats: int
    sql_validate_iterations: int


STANDARD = Sizes(
    analysis_rows=(1_000, 10_000, 50_000),
    sql_rows=(10_000, 100_000),
    retrieval_chunks=(200, 2_000),
    pdf_pages=(10, 60),
    repeats=5,
    sql_validate_iterations=500,
)
QUICK = Sizes(
    analysis_rows=(200,),
    sql_rows=(500,),
    retrieval_chunks=(30,),
    pdf_pages=(2,),
    repeats=2,
    sql_validate_iterations=20,
)


@dataclass(frozen=True)
class Case:
    """One measurable operation: generator setup builds state once, ``run`` is what is timed."""

    name: str
    size: int
    unit: str
    iterations: int
    run: Callable[[], object]
    cleanup: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class Benchmark:
    name: str
    description: str
    build: Callable[[Sizes, Path], Iterator[Case]]


def synthetic_transactions(rows: int, seed: int = SEED) -> pd.DataFrame:
    """Seeded transaction-like table with date, id, category, numeric, and status columns."""
    rng = np.random.default_rng(seed)
    amounts = np.round(rng.lognormal(mean=4.5, sigma=0.9, size=rows), 2)
    spikes = rng.random(rows) < 0.01
    amounts[spikes] *= 12
    return pd.DataFrame(
        {
            "transaction_date": (
                pd.Timestamp("2025-01-01") + pd.to_timedelta(rng.integers(0, 540, rows), unit="D")
            ).strftime("%Y-%m-%d"),
            "customer_id": [f"C{value:05d}" for value in rng.integers(0, max(rows // 5, 10), rows)],
            "merchant": [f"Merchant {value}" for value in rng.integers(0, 80, rows)],
            "region": rng.choice(_REGIONS, rows),
            "amount": amounts,
            "status": rng.choice(_STATUSES, rows),
        }
    )


def synthetic_sentences(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [
        " ".join(rng.choice(_VOCABULARY) for _ in range(rng.randint(12, 22))).capitalize() + "."
        for _ in range(count)
    ]


def synthetic_pdf(pages: int, seed: int) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    sentences = synthetic_sentences(pages * 12, seed)
    for page in range(pages):
        y = 800
        for sentence in sentences[page * 12 : (page + 1) * 12]:
            pdf.drawString(40, y, sentence[:110])
            y -= 18
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _analysis_cases(sizes: Sizes, workdir: Path) -> Iterator[Case]:
    for rows in sizes.analysis_rows:
        dataframe = synthetic_transactions(rows)
        profile = profile_dataframe(dataframe)
        mapping = map_schema(profile)
        features = recommend_anomaly_features(profile, mapping)

        def profile_and_map(frame: pd.DataFrame = dataframe) -> object:
            return map_schema(profile_dataframe(frame))

        def bundle(
            frame: pd.DataFrame = dataframe,
            frame_profile=profile,
            frame_mapping=mapping,
            frame_features=features,
        ) -> object:
            return build_analysis_bundle(
                dataframe=frame,
                profile=frame_profile,
                schema_mapping=frame_mapping,
                anomaly_features=frame_features,
                anomaly_contamination=0.02,
                document_status=None,
            )

        yield Case(f"analysis_profile/rows={rows}", rows, "rows", 1, profile_and_map)
        yield Case(f"analysis_bundle/rows={rows}", rows, "rows", 1, bundle)


@contextmanager
def _database(rows: int, workdir: Path) -> Iterator[Path]:
    path = workdir / f"bench-{rows}.sqlite"
    SQLiteStore(path).save_dataframe(synthetic_transactions(rows))
    yield path


def _sql_cases(sizes: Sizes, workdir: Path) -> Iterator[Case]:
    columns = {DEFAULT_TABLE_NAME: set(synthetic_transactions(5).columns)}
    tables = {DEFAULT_TABLE_NAME}
    for label, query in SQL_QUERIES.items():

        def validate(query: str = query) -> object:
            return validate_read_query(query)

        yield Case(
            f"sql_validate/{label}", len(query), "chars", sizes.sql_validate_iterations, validate
        )
    for rows in sizes.sql_rows:
        with _database(rows, workdir) as database:
            for label, query in SQL_QUERIES.items():

                def execute(query: str = query, database: Path = database) -> object:
                    return execute_read_query(
                        database,
                        query,
                        max_rows=500,
                        allowed_tables=tables,
                        allowed_columns=columns,
                    )

                yield Case(f"sql_execute/{label}/rows={rows}", rows, "rows", 1, execute)


def _retrieval_cases(sizes: Sizes, workdir: Path) -> Iterator[Case]:
    embedder = HashingEmbedder()
    questions = [
        "What approval threshold requires manager review?",
        "How are refund exceptions escalated?",
        "Which vendor payment controls does the audit cover?",
    ]
    for count in sizes.retrieval_chunks:
        sentences = synthetic_sentences(count * 4, seed=count)
        pages = [
            _Page(index + 1, " ".join(sentences[index * 4 : (index + 1) * 4]))
            for index in range(count)
        ]
        chunks = chunk_document_pages(pages, "bench.pdf", document_id=f"bench-{count}")
        store = ChromaDocumentStore(workdir / f"chroma-{count}", embedder)
        store.replace_chunks(chunks)
        for hybrid in (False, True):
            retriever = DocumentRetriever(store, max_distance=None, default_top_k=4, hybrid=hybrid)

            def ask(retriever: DocumentRetriever = retriever) -> object:
                return [retriever.retrieve(question) for question in questions]

            mode = "hybrid" if hybrid else "dense"
            yield Case(
                f"retrieval_{mode}/chunks={len(chunks)}",
                len(chunks),
                "chunks",
                len(questions),
                ask,
                cleanup=store.close if hybrid else (lambda: None),
            )
        builds = {"runs": 0}

        def rebuild(chunks=chunks, count: int = count, builds=builds) -> object:
            builds["runs"] += 1
            scratch = ChromaDocumentStore(
                workdir / f"chroma-build-{count}-{builds['runs']}", embedder
            )
            try:
                scratch.replace_chunks(chunks)
            finally:
                scratch.close()
            return None

        yield Case(f"retrieval_index_build/chunks={len(chunks)}", len(chunks), "chunks", 1, rebuild)


@dataclass(frozen=True)
class _Page:
    page_number: int
    text: str


def _pdf_cases(sizes: Sizes, workdir: Path) -> Iterator[Case]:
    for pages in sizes.pdf_pages:
        payload = synthetic_pdf(pages, seed=pages)
        document = load_pdf_file(io.BytesIO(payload), "bench.pdf")
        chunks = chunk_document_pages(document.pages, "bench.pdf", document_id="bench")
        counter = {"runs": 0}

        def extract(payload: bytes = payload) -> object:
            return load_pdf_file(io.BytesIO(payload), "bench.pdf")

        def chunk(document=document) -> object:
            return chunk_document_pages(document.pages, "bench.pdf", document_id="bench")

        def embed_and_store(chunks=chunks, pages: int = pages, counter=counter) -> object:
            counter["runs"] += 1
            store = ChromaDocumentStore(
                workdir / f"pdf-embed-{pages}-{counter['runs']}", HashingEmbedder()
            )
            try:
                store.replace_chunks(chunks)
            finally:
                store.close()
            return None

        def end_to_end(payload: bytes = payload, pages: int = pages, counter=counter) -> object:
            counter["runs"] += 1
            service = DocumentApplicationService(lambda _model: HashingEmbedder())
            result = service.index(
                IndexDocumentsCommand(
                    documents=(DocumentPayload("bench.pdf", payload),),
                    persist_dir=workdir / f"pdf-e2e-{pages}-{counter['runs']}",
                    embedding_model="benchmark",
                    max_pages=10_000,
                    max_chunks=100_000,
                    retrieval_top_k=4,
                    retrieval_max_distance=None,
                )
            )
            result.retriever.close()
            return None

        yield Case(f"pdf_extract/pages={pages}", pages, "pages", 1, extract)
        yield Case(f"pdf_chunk/pages={pages}", pages, "pages", 1, chunk)
        yield Case(f"pdf_embed_store/pages={pages}", pages, "pages", 1, embed_and_store)
        yield Case(f"pdf_index/pages={pages}", pages, "pages", 1, end_to_end)


BENCHMARKS = (
    Benchmark("analysis", "Profiling and the full deterministic analysis bundle", _analysis_cases),
    Benchmark("sql", "SQL validation and guarded read-only execution", _sql_cases),
    Benchmark("retrieval", "Dense and hybrid retrieval on a Chroma index", _retrieval_cases),
    Benchmark(
        "pdf", "PDF extraction, chunking, embedding+storage, and end-to-end indexing", _pdf_cases
    ),
)


def measure(case: Case, repeats: int) -> dict[str, object]:
    """Time ``case.run`` once to warm up, then ``repeats`` times; report per-call milliseconds."""
    case.run()
    samples: list[float] = []
    for _ in range(repeats):
        started = perf_counter()
        for _ in range(case.iterations):
            case.run()
        samples.append((perf_counter() - started) * 1_000 / case.iterations)
    ordered = sorted(samples)
    return {
        "name": case.name,
        "size": case.size,
        "unit": case.unit,
        "repeats": repeats,
        "iterations_per_repeat": case.iterations,
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "max_ms": round(ordered[-1], 3),
        "ms_per_unit": round(statistics.median(ordered) / max(case.size, 1), 5),
    }


def environment() -> dict[str, object]:
    def version(package: str) -> str:
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return "unavailable"

    commit = "unknown"
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "logical_cpus": os.cpu_count(),
        "pandas": version("pandas"),
        "numpy": version("numpy"),
        "scikit-learn": version("scikit-learn"),
        "chromadb": version("chromadb"),
        "commit": commit,
    }


def selected_benchmarks(only: Sequence[str] | None) -> list[Benchmark]:
    if not only:
        return list(BENCHMARKS)
    wanted = set(only)
    known = {benchmark.name for benchmark in BENCHMARKS}
    unknown = wanted - known
    if unknown:
        raise SystemExit(
            f"Unknown benchmark(s): {', '.join(sorted(unknown))}. Known: {sorted(known)}"
        )
    return [benchmark for benchmark in BENCHMARKS if benchmark.name in wanted]


def run(
    sizes: Sizes,
    only: Sequence[str] | None = None,
    profile_case: str | None = None,
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    profile_output = ""
    with tempfile.TemporaryDirectory(prefix="bench-", ignore_cleanup_errors=True) as directory:
        workdir = Path(directory)
        for benchmark in selected_benchmarks(only):
            for case in benchmark.build(sizes, workdir):
                try:
                    if profile_case is None:
                        results.append({"group": benchmark.name, **measure(case, sizes.repeats)})
                    elif case.name == profile_case:
                        profile_output = profile(case)
                finally:
                    case.cleanup()
    if profile_case is not None and not profile_output:
        raise SystemExit(f"No benchmark case named {profile_case!r}; use --list.")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": SEED,
        "mode": "quick" if sizes is QUICK else "standard",
        "environment": environment(),
        "results": results,
        "profile": profile_output,
    }


def profile(case: Case, limit: int = 20) -> str:
    """cProfile one case (after a warm-up run) and return the top cumulative-time entries."""
    case.run()
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(case.iterations):
        case.run()
    profiler.disable()
    buffer = io.StringIO()
    stats = pstats.Stats(profiler, stream=buffer).sort_stats("cumulative")
    stats.print_stats(limit)
    return buffer.getvalue()


def render_markdown(report: dict[str, object]) -> str:
    environment_info = report["environment"]
    assert isinstance(environment_info, dict)
    lines = [
        f"Benchmarks ({report['mode']}, seed {report['seed']}, {report['generated_at']})",
        "",
        "Environment: " + ", ".join(f"{key}={value}" for key, value in environment_info.items()),
        "",
        "| Group | Case | Size | Median ms | Min ms | Max ms | ms/unit |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    results = report["results"]
    assert isinstance(results, list)
    for row in results:
        lines.append(
            f"| {row['group']} | {row['name']} | {row['size']:,} {row['unit']} | "
            f"{row['median_ms']:,.2f} | {row['min_ms']:,.2f} | {row['max_ms']:,.2f} | "
            f"{row['ms_per_unit']:.4f} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--quick", action="store_true", help="tiny sizes for a fast sanity run")
    parser.add_argument(
        "--only", action="append", help="benchmark group (analysis|sql|retrieval|pdf)"
    )
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    parser.add_argument("--profile", metavar="CASE", help="cProfile one case instead of timing it")
    parser.add_argument("--list", action="store_true", help="list benchmark groups and exit")
    args = parser.parse_args(argv)

    if args.list:
        for benchmark in BENCHMARKS:
            print(f"{benchmark.name}: {benchmark.description}")
        return 0

    sizes = QUICK if args.quick else STANDARD
    report = run(sizes, only=args.only, profile_case=args.profile)
    if args.profile:
        print(report["profile"])
        return 0
    print(render_markdown(report))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
