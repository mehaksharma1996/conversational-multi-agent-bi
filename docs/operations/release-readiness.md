# Release readiness and live-model evaluation

Status as of 2026-10-11 on branch `demo/grounded-bi-experience`. This page separates what has been
verified on this machine from what has not. Nothing here is a claim about a live model: no paid
provider request has been made for this assessment.

## What is and is not verified

| Capability | Status | Evidence |
|---|---|---|
| Deterministic evaluation gate | Verified locally | `python -m scripts.run_evaluations`, see the project report for exact counts |
| Real-stack browser journeys with the **scripted** provider | Verified locally | `npm run test:e2e:real`, 20 Chromium journeys, including the mapper defaults |
| Live Gemini answers to the nine demo questions | **NOT RUN** | Needs an approved budget; see below |
| Container build, nginx, compose smoke, Docker-only Playwright | **BLOCKED locally** | Docker is not installed on this machine; CI's `containers` job is the only check of these paths |
| Image vulnerability scan | **BLOCKED locally** | Runs in CI's `security` job only |
| Public hosted demo | **NOT READY** | See the assessment below |

A scripted-provider run proves the application handles correct model behaviour. It is never evidence
that a real model produces that behaviour.

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

### What each dimension means

Passing one dimension is not evidence for another, so each is scored on its own:

| Dimension | How it is decided |
|---|---|
| routing | The route equals the expected one (`sql`, `rag`, `hybrid`) |
| sql_syntax | SQLite can plan the model's SQL against the real table |
| sql_safety | The application's read-only validator accepts it |
| sql_execution | The message completed with a result |
| sql_semantic | Every value the ground truth requires appears among the returned cells |
| answer_numeric | The same values appear in the answer text |
| retrieval_relevance | At least one passage passed the distance filter |
| citation_structure | No invalid citations or unverified quotes, and sources are present |
| groundedness | Only for the policy-sentence question: the exact sentence is quoted and the structural check is clean |
| policy_sql_consistency | Hybrid questions: the SQL was structurally verified against the policy criteria **and** returned the right count |
| task_completion | Completed, and every applicable dimension above passed |

### Limits of the method

- One run of one model is a sample, not a rate.
- "Semantic" correctness means the expected values are present in the result; a query can contain
  them and still be wrong in another way.
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

Docker is not installed on the machine used for this assessment, so image builds, nginx behaviour,
the compose smoke test, and the Docker-specific browser journeys were **not run locally**. What was
checked statically: `tests/test_container_config.py` and `tests/test_web_demo_assets.py` pass; the
web image still uses `nginxinc/nginx-unprivileged:1.30-alpine-slim` (the PR #99 fix for
CVE-2026-4775) and a digest-pinned Node build stage; the nginx config sends a restrictive
Content-Security-Policy, allows 60 MB bodies in front of the API's exact limits, and serves
`/demo/*` from static files. CI's `containers` job builds both images, enforces size budgets, runs
`scripts/compose_smoke.py`, and its `security` job scans dependencies; those are the independent
checks of these paths.

## Public deployment assessment

The repository is a local application. The project's own documents say to keep it loopback-only
([threat model](../security/threat-model.md), [local containers](local-containers.md)). Concrete
blockers for an externally reachable demo:

1. **No user authentication in the shipped stack.** `API_AUTH_MODE=local` gives every visitor the
   same fixed tenant with all capabilities. Browser sign-in (issue #9, parts d to f) is not
   delivered, so there is no demo-account isolation: visitors would share one tenant.
2. **Anyone could spend your model key.** Rate limits are per process and generous by default
   (120 messages per minute), and there is no per-visitor quota or cost cap.
3. **State on ephemeral hosts.** Workspaces, audit logs, and vectors live on disk. A host with an
   ephemeral filesystem loses them on restart, which defeats workspace recovery.
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

## Release integration

- The branch is based on the head of PR #99 (`ccf5a2c`), so it **contains that CVE fix**. Merge
  #99 first; the shared commit then drops out of this branch's diff.
- The branch also carries a local copy of the usage-guardrail documentation commit
  (`2b672e4`), which duplicates `72c760a` on `docs/usage-guardrail-ask-user`. Merge that docs branch
  first or expect an identical-content conflict.
- PR #97 (numeric formats) is **not** in this branch. Its `containers` check fails because it
  predates #99; rebase it after #99 lands. The demo CSV is clean, so the demo does not depend on it.
- `main` CI is red at `60e3961` for the same CVE, and `main` has no branch protection.
- Suggested order: #99, the guardrail docs branch, #97 (rebased), then this branch as one PR.
