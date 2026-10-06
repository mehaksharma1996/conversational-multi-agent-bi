"""Measure latency and peak memory of the long-running operations at configured-limit sizes (#10).

Each probe runs in a fresh subprocess so peak resident memory (RSS) is attributable to that
operation, not to an earlier one. Peak RSS is sampled from the operating system while the operation
runs, so native allocations (Chroma, torch, kaleido's browser is a child process and is *not*
counted) are included. Nothing is downloaded: the real-embedder probes need the model already cached
(``python -m scripts.prefetch_model``) and are opt-in.

    python -m scripts.measure_limits --list
    python -m scripts.measure_limits                          # deterministic probes
    python -m scripts.measure_limits --real-embedder          # also the SentenceTransformer probes
    python -m scripts.measure_limits --only analysis_request  # one probe family
    python -m scripts.measure_limits --output limits.json

Results are indicative and machine-dependent. The script reports; it never enforces a budget, and
it carries sizes, timings, and memory only (never generated rows, text, or SQL).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = "PROBE_RESULT "
MB = 1024 * 1024


def current_rss_bytes() -> int | None:
    """Resident set size of this process right now, or None when unavailable."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return int(counters.WorkingSetSize) if ok else None
    statm = Path("/proc/self/statm")
    if statm.exists():
        return int(statm.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak) if sys.platform == "darwin" else int(peak) * 1024
    except (ImportError, OSError):
        return None


class PeakSampler:
    """Track the maximum resident set size seen while a block runs (20 ms sampling)."""

    def __init__(self) -> None:
        self.baseline = current_rss_bytes() or 0
        self.peak = self.baseline
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(0.02):
            self.peak = max(self.peak, current_rss_bytes() or 0)

    def __enter__(self) -> PeakSampler:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, current_rss_bytes() or 0)

    @property
    def delta_mb(self) -> float:
        return round(max(self.peak - self.baseline, 0) / MB, 1)

    @property
    def peak_mb(self) -> float:
        return round(self.peak / MB, 1)


@dataclass(frozen=True)
class Probe:
    name: str
    family: str
    needs_real_embedder: bool
    run: Callable[[], dict[str, object]]


def _stage(timings: dict[str, float], name: str, started: float) -> None:
    timings[name] = round(perf_counter() - started, 3)


def _csv_payload(rows: int, labelled: bool = False) -> bytes:
    """Stream a seeded CSV without holding a DataFrame, so memory reflects the operation."""
    import random

    rng = random.Random(20261006)
    regions = ("East", "West", "North", "South", "Central")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header = ["transaction_date", "customer_id", "merchant", "region", "amount", "status"]
    if labelled:
        header += ["account_age_days", "label"]
    writer.writerow(header)
    for index in range(rows):
        amount = round(rng.lognormvariate(4.5, 0.9), 2)
        row: list[object] = [
            f"2025-{1 + index % 12:02d}-{1 + index % 28:02d}",
            f"C{rng.randrange(max(rows // 5, 10)):06d}",
            f"Merchant {rng.randrange(80)}",
            rng.choice(regions),
            amount,
            rng.choice(("approved", "approved", "declined", "pending")),
        ]
        if labelled:
            age = rng.randrange(1, 2000)
            row += [age, int(rng.random() < min(0.05 + amount / 4000 + (200 / (age + 50)), 0.9))]
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def probe_analysis_request(rows: int) -> dict[str, object]:
    from packages.analytics.contracts import (
        AnalyzeTabularCommand,
        LoadTabularCommand,
        ProfileTabularCommand,
    )
    from packages.analytics.service import TabularApplicationService

    payload = _csv_payload(rows)
    service = TabularApplicationService()
    timings: dict[str, float] = {}
    with PeakSampler() as sampler:
        started = perf_counter()
        table = service.load(
            LoadTabularCommand(
                payload=payload,
                filename="bench.csv",
                max_upload_bytes=len(payload) + 1,
                max_rows=rows + 1,
            )
        )
        _stage(timings, "load_s", started)
        started = perf_counter()
        profiled = service.profile(ProfileTabularCommand(dataframe=table.dataframe))
        _stage(timings, "profile_s", started)
        started = perf_counter()
        service.analyze(
            AnalyzeTabularCommand(
                dataframe=table.dataframe,
                profile=profiled.profile,
                schema_mapping=profiled.suggested_mapping,
                anomaly_features=profiled.recommended_anomaly_features,
                anomaly_contamination=0.02,
                document_status=None,
            )
        )
        _stage(timings, "analyze_s", started)
    return {
        "size": rows,
        "unit": "rows",
        "payload_mb": round(len(payload) / MB, 1),
        "total_s": round(sum(timings.values()), 3),
        **timings,
        "peak_rss_mb": sampler.peak_mb,
        "rss_growth_mb": sampler.delta_mb,
    }


def probe_classifier(rows: int) -> dict[str, object]:
    from src.analytics.supervised_classification import run_supervised_classification
    from src.ingestion.tabular_loader import load_tabular_file
    from src.profiling.data_profiler import profile_dataframe
    from src.profiling.schema_mapper import map_schema

    payload = _csv_payload(rows, labelled=True)
    table = load_tabular_file(io.BytesIO(payload), "bench.csv", max_rows=rows + 1)
    profile = profile_dataframe(table.dataframe)
    mapping = map_schema(profile, overrides={"label": "label"})  # a confirmed label
    with PeakSampler() as sampler:
        started = perf_counter()
        report = run_supervised_classification(table.dataframe, profile, mapping)
        elapsed = perf_counter() - started
    return {
        "size": rows,
        "unit": "rows",
        "classifier_enabled": bool(report.enabled),
        "total_s": round(elapsed, 3),
        "peak_rss_mb": sampler.peak_mb,
        "rss_growth_mb": sampler.delta_mb,
    }


def probe_report_render() -> dict[str, object]:
    from packages.analytics.contracts import (
        AnalyzeTabularCommand,
        LoadTabularCommand,
        ProfileTabularCommand,
    )
    from packages.analytics.service import TabularApplicationService
    from src.reporting.pdf_report import build_report_pdf

    payload = _csv_payload(10_000)
    service = TabularApplicationService()
    table = service.load(
        LoadTabularCommand(
            payload=payload,
            filename="bench.csv",
            max_upload_bytes=len(payload) + 1,
            max_rows=10_001,
        )
    )
    profiled = service.profile(ProfileTabularCommand(dataframe=table.dataframe))
    bundle = service.analyze(
        AnalyzeTabularCommand(
            dataframe=table.dataframe,
            profile=profiled.profile,
            schema_mapping=profiled.suggested_mapping,
            anomaly_features=profiled.recommended_anomaly_features,
            anomaly_contamination=0.02,
            document_status=None,
        )
    )
    charts = list(bundle.chart_specs)
    timings: dict[str, float] = {}
    with PeakSampler() as sampler:
        started = perf_counter()
        build_report_pdf(bundle.business_report, None)
        _stage(timings, "without_charts_s", started)
        started = perf_counter()
        with_charts = build_report_pdf(bundle.business_report, charts)
        _stage(timings, "with_charts_cold_s", started)
        started = perf_counter()
        build_report_pdf(bundle.business_report, charts)
        _stage(timings, "with_charts_warm_s", started)
    return {
        "size": len(charts),
        "unit": "charts",
        "pdf_kb": round(len(with_charts) / 1024),
        "total_s": timings["with_charts_cold_s"],
        **timings,
        "peak_rss_mb": sampler.peak_mb,
        "rss_growth_mb": sampler.delta_mb,
    }


def probe_pdf_index(pages: int, real_embedder: bool) -> dict[str, object]:
    import tempfile

    from packages.evaluation.fakes import HashingEmbedder
    from packages.retrieval.service import (
        DocumentApplicationService,
        DocumentPayload,
        IndexDocumentsCommand,
    )
    from scripts.run_benchmarks import synthetic_pdf

    timings: dict[str, float] = {}
    embedder: object
    if real_embedder:
        from src.documents.embedding import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder()
        started = perf_counter()
        embedder.embed_texts(["warm up the model"])
        _stage(timings, "model_load_s", started)
    else:
        embedder = HashingEmbedder()
    payload = synthetic_pdf(pages, seed=pages)
    service = DocumentApplicationService(lambda _model: embedder)
    with tempfile.TemporaryDirectory(prefix="limits-", ignore_cleanup_errors=True) as directory:
        with PeakSampler() as sampler:
            started = perf_counter()
            result = service.index(
                IndexDocumentsCommand(
                    documents=(DocumentPayload("bench.pdf", payload),),
                    persist_dir=Path(directory) / "vectorstore",
                    embedding_model="benchmark",
                    max_pages=100_000,
                    max_chunks=1_000_000,
                    retrieval_top_k=4,
                    retrieval_max_distance=None,
                )
            )
            _stage(timings, "index_s", started)
        chunks = result.chunk_count
        result.retriever.close()
    return {
        "size": pages,
        "unit": "pages",
        "chunks": chunks,
        "pdf_kb": round(len(payload) / 1024),
        "total_s": timings["index_s"],
        **timings,
        "peak_rss_mb": sampler.peak_mb,
        "rss_growth_mb": sampler.delta_mb,
    }


PROBES: tuple[Probe, ...] = (
    Probe(
        "analysis_request/rows=100000",
        "analysis_request",
        False,
        lambda: probe_analysis_request(100_000),
    ),
    Probe(
        "analysis_request/rows=500000",
        "analysis_request",
        False,
        lambda: probe_analysis_request(500_000),
    ),
    Probe("classifier/rows=50000", "classifier", False, lambda: probe_classifier(50_000)),
    Probe("report_render/charts", "report_render", False, probe_report_render),
    Probe(
        "pdf_index/pages=500/embedder=hashing",
        "pdf_index",
        False,
        lambda: probe_pdf_index(500, False),
    ),
    Probe(
        "pdf_index/pages=60/embedder=real",
        "pdf_index",
        True,
        lambda: probe_pdf_index(60, True),
    ),
    Probe(
        "pdf_index/pages=500/embedder=real",
        "pdf_index",
        True,
        lambda: probe_pdf_index(500, True),
    ),
)


def run_child(name: str) -> int:
    probe = next((candidate for candidate in PROBES if candidate.name == name), None)
    if probe is None:
        print(f"Unknown probe {name!r}.", file=sys.stderr)
        return 2
    print(RESULT_PREFIX + json.dumps(probe.run()))
    return 0


def run_probe(probe: Probe, timeout_seconds: int) -> dict[str, object]:
    started = perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.measure_limits", "--child", probe.name],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"name": probe.name, "status": "timeout", "timeout_s": timeout_seconds}
    for line in completed.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line[len(RESULT_PREFIX) :])
            return {
                "name": probe.name,
                "status": "ok",
                "wall_s": round(perf_counter() - started, 1),
                **payload,
            }
    return {"name": probe.name, "status": "failed", "exit_code": completed.returncode}


def render_markdown(report: dict[str, object]) -> str:
    lines = [
        f"Limits ({report['generated_note']})",
        "",
        "| Probe | Size | Total s | Peak RSS MB | RSS growth MB | Stages / notes |",
        "|---|---:|---:|---:|---:|---|",
    ]
    skip = {
        "name",
        "status",
        "size",
        "unit",
        "total_s",
        "peak_rss_mb",
        "rss_growth_mb",
        "wall_s",
    }
    for row in report["results"]:  # type: ignore[attr-defined]
        if row["status"] != "ok":
            lines.append(f"| {row['name']} | | | | | {row['status']} |")
            continue
        notes = ", ".join(f"{key}={value}" for key, value in row.items() if key not in skip)
        lines.append(
            f"| {row['name']} | {row['size']:,} {row['unit']} | {row['total_s']} | "
            f"{row['peak_rss_mb']} | {row['rss_growth_mb']} | {notes} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--only", action="append", help="probe family to run")
    parser.add_argument("--real-embedder", action="store_true", help="include real-model probes")
    parser.add_argument("--timeout", type=int, default=1_800, help="seconds per probe")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.child:
        return run_child(args.child)
    if args.list:
        for probe in PROBES:
            note = " (opt-in: --real-embedder)" if probe.needs_real_embedder else ""
            print(f"{probe.name}{note}")
        return 0

    selected = [
        probe
        for probe in PROBES
        if (args.real_embedder or not probe.needs_real_embedder)
        and (not args.only or probe.family in args.only)
    ]
    results = [run_probe(probe, args.timeout) for probe in selected]
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_note": f"{platform.platform()}, CPython {platform.python_version()}",
        "logical_cpus": os.cpu_count(),
        "results": results,
    }
    print(render_markdown(report))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if all(row["status"] == "ok" for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
