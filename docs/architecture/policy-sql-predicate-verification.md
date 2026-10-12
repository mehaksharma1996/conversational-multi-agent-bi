# Policy-to-SQL predicate verification

Hybrid questions cross a trust boundary: a model turns document-derived policy criteria and a
user question into executable SQL. Read-only SQL validation prevents mutation, but it does not
prove that the query applies the right business filters. The predicate verifier therefore runs
after the read-only guard and before approval or execution.

## Trust boundaries

| Predicate source | Authority | How it is established |
|---|---|---|
| Policy | Retrieved document | A strict structured-output model is sanitized against a specific redacted excerpt, table field allowlist, operator evidence, literal value, and unit |
| User | Original question | The parsed SQL field and literal must both be explicit in the question; inequalities also require compatible comparison wording |
| System | Application code | A typed `AuthorizedSQLPredicate` is passed by trusted code; model output and request payloads cannot create one |
| Model-only | None | Any remaining business predicate is unjustified and causes refusal |

Tenant and workspace ownership are normally enforced before orchestration and by per-workspace
storage, not by model-generated SQL. The explicit system-predicate input exists for deployments
that add a mandatory row-level constraint; tests prove such constraints are distinguishable from
model-added filters.

## Bounded verification algorithm

`src/orchestration/policy_grounding.py` uses `sqlparse` to locate one top-level `WHERE` clause and
build a small boolean expression tree. It supports direct field-to-literal comparisons, reversed
literal comparisons, `IS NULL`, `IS NOT NULL`, `AND`, `OR`, and parentheses. Numeric literals are
compared as decimals, so `0.30` and `0.3` are equivalent without converting units.

The verifier then:

1. builds the required policy expression, including `all`/`any` joins and explicit policy null
   inclusion;
2. proves that expression is a top-level conjunct of the SQL filter;
3. inventories every remaining predicate; and
4. attributes each remainder to the question, a trusted system predicate, or a redundant null
   exclusion required by the policy.

An extra condition joined with `AND` still fails if it has no authority. A required condition under
the wrong `OR`, an unexpected negation, a function-wrapped field, arithmetic, `BETWEEN`, `IN`, a
subquery, or another unsupported form returns `unverified`. `mismatch`, `conflicting_evidence`, and
`unverified` all route to the existing document-only fallback before approval or execution. The
application never deletes a condition and executes rewritten SQL.

## Status meaning

- `structurally_verified`: all required policy predicates and every supported business filter were
  accounted for. This is predicate completeness for the supported grammar, not full semantic
  equivalence.
- `mismatch`: a policy predicate is missing or structurally wrong, or an extra predicate is
  unjustified.
- `conflicting_evidence`: source-attributed policy predicates conflict.
- `unverified`: the SQL expression is outside the bounded grammar or predicate attribution is
  ambiguous.
- `not_evaluated`: no source-attributed structured predicates were available.

The exact internal issue tuple is retained in graph state for deterministic handling and tests.
Telemetry and the API expose only safe statuses, counts, and route-decision facts; they never emit
SQL, questions, literals, document text, prompts, or model output.

## Known limitations

This checker is deliberately not a SQL theorem prover. It does not prove equivalence across
distributive rewrites, casts, functions, arithmetic, collations, subqueries, aliases derived from
expressions, or dialect-specific operators. User-filter attribution is lexical and conservative;
an implicit or unusually phrased filter can be refused even when a person would understand it.
Those cases should be expressed more explicitly or added through a typed trusted constraint, not
made executable by weakening the verifier.
