# Benchmarks and indicative baselines

`python -m scripts.run_benchmarks` measures the deterministic core on seeded synthetic data using only
the standard library for timing (`perf_counter`, `statistics`, `cProfile`). It downloads nothing and
calls no model: embeddings come from the offline hashing embedder used by the evaluation harness, so the
numbers isolate this repository's code, not a provider or the real SentenceTransformer.

```powershell
python -m scripts.run_benchmarks                       # standard sizes, markdown table
python -m scripts.run_benchmarks --output bench.json   # plus a machine-readable report
python -m scripts.run_benchmarks --quick               # tiny sizes (what the unit test runs)
python -m scripts.run_benchmarks --only sql            # analysis | sql | retrieval | pdf
python -m scripts.run_benchmarks --only analysis --profile analysis_profile/rows=50000
```

Each case runs once to warm up, then five times; the report gives the median, min, and max in
milliseconds per call. Reports contain sizes and timings only, never generated text, rows, or SQL.

These are **indicative baselines, not gates.** CI runs the script in a separate `benchmarks` job marked
`continue-on-error`, uploads the report as an artifact, and writes the table to the job summary.
Shared runners are noisy, so no threshold is enforced anywhere; compare by eye and re-run locally
before concluding anything. `tests/test_benchmarks.py` only checks that the runner works, its output
shape, determinism of the data, and that no content reaches the report.

## Baseline (2026-10-06, commit f252cef)

Machine: Windows 11 (10.0.26300), AMD64 Family 25 Model 80 (12 logical CPUs), CPython 3.14.3,
pandas 3.0.6, numpy 2.5.3, scikit-learn 1.9.1, chromadb 1.5.9. Seed 20261006. Medians in ms.

### Analysis pipeline

| Case | Rows | Median ms |
|---|---:|---:|
| `analysis_profile` (profile + schema map) | 1,000 | 32.8 |
| | 10,000 | 227.1 |
| | 50,000 | 1,137.2 |
| `analysis_bundle` (analytics, anomalies, charts, report) | 1,000 | 328.4 |
| | 10,000 | 502.7 |
| | 50,000 | 1,051.9 |

The synthetic frame has no binary label column, so the supervised-classifier path (#30) is not
exercised; only its eligibility check runs.

### SQL validation and guarded execution (`max_rows=500`, table/column allowlists on)

| Case | Median ms |
|---|---:|
| `validate_read_query` filter / group-by / window CTE | 0.6 / 1.9 / 2.0 |
| execute at 10,000 rows: filter / group-by / window CTE | 2.8 / 7.7 / 13.2 |
| execute at 100,000 rows: filter / group-by / window CTE | 12.2 / 73.0 / 143.7 |

### Retrieval (Chroma, hashing embedder, three questions per call; ms is per question)

| Chunks | Dense | Hybrid (dense + BM25 + RRF) | Index build (total) |
|---:|---:|---:|---:|
| 169 | 8.5 | 10.6 | 385 |
| 1,693 | 9.7 | 20.1 | 2,322 |

### PDF indexing (synthetic ReportLab PDFs, hashing embedder)

| Pages | Extract | Chunk | Embed + store | End to end |
|---:|---:|---:|---:|---:|
| 10 | 21.9 | 0.4 | 254.3 | 278.4 |
| 60 | 152.3 | 2.8 | 396.5 | 554.6 |

## Findings (cProfile on the surprising results)

1. **`profile_dataframe` is the largest avoidable cost in the analysis path and is linear in rows.** At
   50,000 rows it takes ~1.1 s, and the profile shows ~2.5 s inside `pandas.to_datetime(...,
   format="mixed")` (profiled run), reached from `_is_date_like`, which falls back to per-value
   `dateutil`/`strptime` parsing for every string column that is not numeric. The whole column is
   parsed only to decide whether ≥ a threshold of values look like dates. Judging a bounded sample
   would remove almost all of this. That changes profiling behavior (and possibly column typing on
   edge cases), so it is recorded here, not changed in this slice.
2. **`analysis_bundle` has ~0.3 s of fixed overhead even at 1,000 rows.** In the profiled run about half is Plotly
   figure construction (`build_charts`, dominated by `update_layout`/figure validation) and about 40%
   is IsolationForest fit/predict. Past ~10,000 rows the cost grows slowly, so for typical uploads the
   end-to-end request is a few hundred milliseconds of compute plus the profile step above.
3. **SQL validation is ~0.6–2 ms per query and is almost entirely `sqlparse`.** That is negligible next
   to model latency and to execution at 100,000 rows (12–144 ms), so it is not a hot spot. Window
   queries are the slowest class at 100,000 rows.
4. **Hybrid retrieval costs ~2× dense at 1.7k chunks** (20 vs 10 ms) because BM25 scoring is linear in
   chunk count while the HNSW dense query is nearly flat. Both are far below model latency; revisit
   only if per-workspace chunk limits (`MAX_DOCUMENT_CHUNKS`) rise by an order of magnitude.
5. **Per-index fixed cost is ~0.24 s, independent of size** (the 2-page quick run and the 10-page run
   both spend ~240–250 ms in embed+store), consistent with creating a Chroma persistent client and
   collection. PDF extraction scales at ~2.2–2.5 ms/page. With the real SentenceTransformer, embedding
   will add a per-chunk cost that this offline harness deliberately does not measure.

## Inputs for issue #10 (not implemented here)

- Request-budget evidence: for the sizes above, deterministic compute (profile + bundle) is ~2.2 s at
  50,000 rows and PDF index ~0.55 s at 60 pages with the hashing embedder, so the long poles in a real
  deployment are the embedding model and Gemini calls, not this repository's deterministic code.
- Not yet measured and needed by #10: peak memory (RSS or `tracemalloc`; Chroma allocations are native
  and invisible to `tracemalloc`), real-embedder indexing time, PDF **report rendering** (ReportLab), and
  the supervised-classifier path on a labelled frame.
- The date-detection sampling opportunity (finding 1) is the one cheap, evidence-backed latency
  improvement to schedule alongside #10's request budgets.
- This benchmark does not decide in-process vs worker execution; it supplies the repeatable harness #10
  can extend (new cases are small `Case` generators in `scripts/run_benchmarks.py`).

## Limits of these numbers

Single machine, single run of five repeats, a laptop with background load, Python 3.14. The hashing
embedder is much cheaper than a transformer. Treat differences under ~20% as noise.
