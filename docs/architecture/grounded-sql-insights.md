# Deterministic SQL insight rendering

## Problem

The guarded SQL workflow previously returned a useful result table but described most multi-row
results only as a row and column count. Asking a language model to narrate the table would produce
more fluent text, but it would also make arithmetic, units, and claims depend on another generation
step that could invent values or imply unsupported causes.

## Decision

SQL and hybrid routes use `src/agents/sql_insights.py` to derive a small structured set of facts
directly from the executed pandas dataframe. The language model continues to propose SQL; it does
not calculate or rewrite authoritative result values.

Each `SQLInsightFact` records:

- a bounded kind (`metric`, `comparison`, or `trend`);
- deterministic text; and
- the exact result row indexes, columns, and formatted values supporting the statement.

The public answer is a rendering of that intermediate representation. The complete result table
and generated SQL remain visible and are the authoritative evidence.

## Supported result shapes

| Shape | Deterministic behavior |
|---|---|
| Empty | States that the query succeeded with no matching rows. |
| One row | Lists every returned metric, including explicit `missing` values. |
| Label plus numeric measure | Identifies the highest and lowest returned values and their difference. |
| Time label plus numeric measure | Orders parseable periods chronologically and reports first-to-last change. |
| Categorical/detail rows | Directs the user to the result table instead of inventing a ranking. |

At most four facts are rendered so a wide result cannot produce an unbounded answer.

## Integrity rules

- Null and non-finite numeric values are excluded from comparisons and disclosed.
- Display values use decimal conversion and at most six decimal places to suppress binary floating
  artifacts. Raw query rows remain available for inspection and export.
- Monetary-looking aliases such as `revenue` or `cost` receive no currency symbol unless the result
  contains an explicit currency/unit field.
- Percentage changes are calculated only when the comparison denominator is non-zero. Relative
  highest-versus-lowest percentages are omitted when the lowest value is zero or negative.
- A causal question receives an explicit limitation: the query can show a difference or association,
  not explain why it occurred.
- No insight text is sent to telemetry or audit attributes.

## Alternatives rejected

### A second LLM narration call

Rejected for the authoritative path because it adds latency, cost, trajectory complexity, and a new
opportunity to alter numbers or introduce unsupported causality. A future optional wording layer may
only consume and preserve the structured facts; it must never replace them.

### Domain-specific hard-coded dashboards

Rejected as the only answer path because uploaded schemas are dynamic. The renderer intentionally
uses conservative result-shape rules that work for the retail demo and arbitrary guarded queries.

### Inferring units from column names

Rejected because `amount`, `revenue`, or `rate` does not prove a currency or storage convention.
The renderer names the ambiguity rather than silently choosing `$`, `%`, or another unit.

## Known limits

The renderer interprets only the executed result, not business meaning outside it. It does not prove
that model-generated SQL answered the intended question, establish causality, or create a semantic
metric layer. Those concerns remain separate evaluation and product responsibilities.

## Verification

`tests/test_sql_insights.py` covers scalar metrics, comparisons, trends, nulls, non-finite and very
large values, ambiguous units, empty results, categorical results, causal questions, booleans, and
input immutability. SQL-agent and LangGraph tests verify integration with the existing guarded path.
The retail-demo contract test runs the committed CSV through ingestion, SQLite persistence, guarded
query execution, and insight rendering, then checks the result against committed ground truth.
