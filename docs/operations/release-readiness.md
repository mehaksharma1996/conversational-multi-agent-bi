# Release readiness and live-model evaluation

Current portfolio status, reconciled with merged `main` at `8d0360ad` on 2026-10-13. This page
separates deterministic, scripted-provider, live-model, container-CI, and deployment evidence. The
controlled free-tier Gemini material and the final integration note are retained as dated historical
records; no additional provider request was made for this documentation update.

## What is and is not verified

| Capability | Status | Evidence |
|---|---|---|
| Deterministic evaluation gate | Verified | 85 versioned cases in `evals/v1/baseline.json`; scripted providers exercise the real guards |
| Real-stack browser journeys with the **scripted** provider | Verified | `npm run test:e2e:real`, 20 Chromium journeys, including the mapper defaults |
| Controlled live Gemini hybrid case | Verified for one run | Four successful HTTP attempts; see the preserved report and evidence below |
| Live Gemini answers to the full demo plan | **NOT RUN** | Only the controlled hybrid case was run; one result is not a rate |
| Container build, nginx, Compose smoke, Docker-only Playwright | Verified in Linux CI; not run on the assessment host | Merged-main run `38340160017` passed the `containers` job; this is not Docker Desktop evidence |
| Image vulnerability scan and SBOM generation | Verified in Linux CI | The same `containers` job generated SBOMs and passed the configured Trivy gates |
| OIDC, browser sessions, roles, CSRF, and auth audit | Verified with deterministic tests | No live identity-provider integration or public deployment has been exercised |
| Durable workspace recovery | Verified for one local API process | Compose enables ADR 0022 metadata/content/index recovery; this is not horizontal scale |
| Prometheus metrics and OTLP traces | Implemented and tested, opt-in | No collector, scraper, dashboard, alert delivery, or hosted monitoring stack is bundled |
| Public hosted demo | **NOT DEPLOYED** | See the assessment below |

A scripted-provider run proves the application handles correct model behaviour. It is never evidence
that a real model produces that behaviour.

### Controlled hybrid evidence from 2026-10-12

The preserved `live-demo-free-tier-controlled-hybrid-rerun-2026-10-12.json` report records one
successful run of the fulfillment escalation case with four model calls and four HTTP attempts. The
initial and final route were both `hybrid`; the extracted source-attributed policy criterion was
`fulfillment_days >= 10`; the generated SQL contained one business predicate and no unjustified
predicate; the policy-to-SQL check was `structurally_verified`; and the query returned exactly 13.
There were no retries, provider fallbacks, provider failures, or quota errors. This is evidence for
that controlled run only, not for the full demo plan or a general success rate.

## Evaluating a real model

`python -m scripts.run_live_demo_eval` drives the real API (upload, schema confirmation, PDF
indexing, conversation) with the configured Gemini model and scores 15 questions: the nine demo
questions against the committed ground truth, one follow-up, one policy-threshold question, two
safety probes, and two exploratory prompts that a person must judge.

Plan only (no client is built, nothing is sent):

```powershell
& ".\.venv\Scripts\python.exe" -m scripts.run_live_demo_eval --max-calls 60 --max-output-tokens 2048 --max-total-tokens 250000
```

A real run needs all of `--execute`, `RUN_LIVE_DEMO_EVALS=1`, a Gemini key in the environment, and
explicit `--max-calls`, `--max-output-tokens`, and `--max-total-tokens`:

```powershell
$env:RUN_LIVE_DEMO_EVALS = "1"
& ".\.venv\Scripts\python.exe" -m scripts.run_live_demo_eval --execute `
  --max-calls 60 --max-output-tokens 2048 --max-total-tokens 250000 `
  --input-price 0.30 --output-price 2.50 --max-cost-usd 0.50
```

The report is written to `evals/results/live-demo.json` (git-ignored) with `mode: "live"`.
Each case also records content-free per-call diagnostics (purpose, success/failure, input/output
token counts, whether usage was reported, and whether output was within 10% of the configured cap)
plus structured-output repair/failure counts. It never records prompts or responses.

### Structured-output evidence from the 2026-10-12 one-case run

The preserved `live-demo-free-tier-routing-v2-2026-10-12.json` report shows five calls, one failed
call, 2,586 prompt tokens, and 4,202 billed output tokens for the fulfillment case under a 2,048
per-call output cap. The application performed one bounded structured-output repair. This proves a
validation failure and high aggregate output use, but the old report did not retain per-call token
counts, finish reasons, or the candidate/thinking split. It therefore does **not** prove response
truncation or isolate Gemini thinking-token consumption.

The smallest evidence-supported change is the per-call diagnostics above. The token cap remains
2,048 and strict local Pydantic validation plus the one-repair bound remain unchanged. A future
single-case rerun can now show whether the criteria call itself approaches the cap; only then should
the prompt/schema or a purpose-specific thinking budget be changed. Do not raise output limits from
the aggregate evidence alone.

### What each dimension means

Passing one dimension is not evidence for another, so each is scored on its own:

| Dimension | How it is decided |
|---|---|
| routing | The route equals the expected one (`sql`, `rag`, `hybrid`) |
| sql_syntax | SQLite can plan the model's SQL against the real table |
| sql_safety | The application's read-only validator accepts it |
| sql_execution | The message completed with a result |
| sql_semantic | Containment: every value the ground truth requires appears among the returned cells |
| sql_result_equivalent | Report schema v2: the returned rows equal the result of the case's oracle `reference_sql` (see below) |
| answer_numeric | The same values appear in the answer text |
| retrieval_relevance | At least one passage passed the distance filter |
| citation_structure | No invalid citations or unverified quotes, and sources are present |
| groundedness | Only for the policy-sentence question: the exact sentence is quoted and the structural check is clean |
| policy_sql_consistency | Hybrid questions: the SQL was structurally verified against the policy criteria **and** returned the right count |
| task_completion | Completed, and every applicable dimension above passed (from schema v2 this includes `sql_result_equivalent` wherever the case has a `reference_sql`) |

### Result-set equivalence (`sql_result_equivalent`)

Added in report schema `live-demo-eval-v2` because containment alone credits a query that returns
the expected cells plus wrong ones. Implemented in `packages/evaluation/result_sets.py`:

- Column counts must match, and columns are compared **by position**; column names (aliases) are
  ignored. Extra columns or a different column order are not equivalent.
- Row counts must match, and rows are paired one-to-one by a maximum matching, so duplicates must
  occur the same number of times and one reference row never matches two returned rows.
- Rows are unordered unless the case sets `result_equivalence.ordered`. Ranking questions
  ("which region has the highest …") are ordered and set `accept_prefix_rows: [1]`, so either the
  full ranking or its first row is accepted, and nothing else.
- Numbers (integers, floats, decimals) match when they differ by **strictly less than 0.005**,
  computed exactly in decimal arithmetic. Nulls match only nulls (NaN is null), booleans only
  booleans (`true` is not `1`), and strings only identical strings (no trimming, case folding, or
  numeric coercion).
- It applies only to cases with a `reference_sql` and only to completed answers; document-only
  cases are `not applicable`. `sql_semantic` is kept unchanged so the two can be compared.

Reports with schema `live-demo-eval-v1` were scored on containment alone; their
`task_completion` is not comparable with v2 reports.

### Limits of the method

- One run of one model is a sample, not a rate.
- `sql_semantic` means the expected values are present in the result; a query can contain them and
  still be wrong in another way. `sql_result_equivalent` closes that gap but is strict about result
  shape: a reasonable answer with extra columns or a different column order does not count.
- Groundedness has no LLM judge; it is deterministic for one case only. The exploratory cases are
  never scored.
- Token counts are provider-reported (Gemini thinking tokens are counted as output, as they are
  billed) or a pessimistic local estimate. Cost is an estimate from the prices you supply.
- Local limits (calls, per-call output cap, total tokens, estimated cost, retries, timeouts, a
  three-consecutive-failure stop) do not replace a spend cap at the provider.

### Cost estimate for the proposed run

The scripted run of the same plan makes 34 model calls with about 48,000 prompt characters in
total (largest prompt 2,900 characters). With retries and SQL corrections, assume up to 60 calls.

| | Expected | Worst case under the proposed limits |
|---|---|---|
| Model calls | about 34 | 60 (up to 120 HTTP requests with one retry each) |
| Input tokens | about 15,000 to 25,000 | about 60,000 |
| Output tokens (including thinking) | about 10,000 to 40,000 | 60 x 2,048 = 122,880, refused earlier by the 250,000 total cap |
| Cost at $0.30 / $2.50 per million | about $0.03 to $0.11 | about $0.33 |

Gemini 2.5 Flash prices are those on <https://ai.google.dev/gemini-api/docs/pricing> on 2026-10-11:
$0.30 per million input tokens and $2.50 per million output tokens including thinking. The page
lists the model as free of charge on the free tier but gives no rate limits there, and the
deprecations page states that 2.5 models stay available "until further notice" for existing users.
The proposed hard local limit is **$0.50** estimated cost. To my knowledge Google Cloud budgets
alert but do not stop spending; check your billing console and prefer a key in a project with a
low quota.

## Containers

Docker was not installed on the machine used for the original demo-branch assessment, so those paths
were not run there. They are nevertheless verified in Linux CI: merged-main run
[`38340160017`](https://github.com/mehaksharma1996/conversational-multi-agent-bi/actions/runs/38340160017)
passed the `containers` job, which builds both digest-pinned, non-root images; enforces size budgets;
generates SBOMs; applies Trivy gates; starts the health-checked Compose topology; exercises hardening,
restart recovery, audit backup/restore, nginx/CSP, browser journeys, and the test-only provider stack;
then tears the stack down. This is strong container evidence, but it is not a local Docker Desktop
run, a live-identity test, or a deployment.

## Public deployment assessment

The repository is a local application. The project's own documents say to keep it loopback-only
([threat model](../security/threat-model.md), [local containers](local-containers.md)). Concrete
blockers for an externally reachable demo:

1. **The default stack is intentionally not public.** `API_AUTH_MODE=local` gives every visitor the
   same fixed tenant with all capabilities. OIDC Bearer verification, authorization-code + PKCE
   browser login, HttpOnly sessions, CSRF/Origin checks, roles/capabilities, and auth audit events are
   implemented and tested, but no live IdP or public-hosting configuration has been validated.
2. **Anyone could spend your model key.** Rate limits are per process and generous by default
   (120 messages per minute), and there is no per-visitor quota or cost cap.
3. **Hosted durability is unsolved.** Workspaces, audit logs, and vectors are durable on the local
   single-node volumes, with tested recovery and backup/restore. An ephemeral host still loses them,
   and the repository does not implement a coordinated multi-process store.
4. **Vector store is unencrypted** under a documented waiver that expires 2027-01-31.
5. **No HSTS header** in the nginx config; TLS termination is the host's job.
6. **Uploads are user-controlled files** processed by the server; the limits exist but no
   anti-abuse layer (per-IP limits, upload scanning) does.

### Hosting options (figures from provider documentation on 2026-10-11)

| Option | What the documentation says | Fit |
|---|---|---|
| Recorded demo or local run | No cost, no exposure | Best until blockers 1 and 2 are closed |
| Fly.io, one Machine plus a volume | No free allowance beyond a 2-hour or 7-day trial; shared-cpu-1x is $3.69 per month at 512 MB and $6.70 at 1 GB; volumes $0.15 per GB per month; outbound traffic $0.02 per GB in North America and Europe | Fits the single-API-container design. The API's memory need (embedding model) has not been measured; budget at least 1 GB and measure first |
| Render free web service | Spins down after 15 minutes; 750 instance hours per month; **no persistent disk**; ephemeral filesystem; RAM and CPU not stated on the page I could read | Unsuitable for workspace recovery |
| Hugging Face Spaces | Docker Spaces require a paid plan; the disk is not persistent | Not free for this app |
| Google Cloud Run | Pricing page text could not be retrieved; figures not verified | Stateless by design; the SQLite and vector-store layout would need rework. Not assessed |

No free service has been shown to have enough resources for this application. Nothing has been
deployed or purchased.

## Historical release integration note

The following bullets record the state and merge advice from the original
`demo/grounded-bi-experience` assessment. They are retained for provenance and are **not current
instructions**: PRs #97, #99, #100, and #101 are now represented in merged `main`, whose audited tip
for this checkpoint is `8d0360ad`.

- The branch is based on the head of PR #99 (`ccf5a2c`), so it **contains that CVE fix**. Merge
  #99 first; the shared commit then drops out of this branch's diff.
- The branch also carries the usage-guardrail documentation as `2b672e4`. It is patch-equivalent to
  `72c760a` on `docs/usage-guardrail-ask-user`; do not merge both copies. Keep the feature copy, or
  merge the docs branch and drop the duplicate while updating the feature branch.
- PR #97 (numeric formats) is **not** in this branch. Its `containers` check fails because it
  predates #99; refresh it after #99 lands and require a green rerun. The demo CSV is clean, so the
  demo does not depend on it.
- `main` CI is red at `60e3961` for the same CVE, and `main` has no branch protection.
- Suggested order: #99, #97 refreshed on the fixed base, then this branch updated from `main`
  without rewriting its published history unless explicitly approved. Treat the standalone
  guardrail branch as superseded if this feature copy is retained.
