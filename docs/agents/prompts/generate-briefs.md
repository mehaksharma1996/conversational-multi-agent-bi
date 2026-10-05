# Codex prompt: generate the remaining issue briefs

Run once, from the repository root, on a clean tree:

```powershell
git switch -c chore/agent-briefs
Get-Content docs/agents/prompts/generate-briefs.md -Raw | codex exec -s workspace-write -c 'model_reasoning_effort="medium"' -
```

Everything below the line is the prompt.

---

You are preparing work briefs so that later AI-agent sessions can finish each GitHub issue in this
repository quickly and cheaply. You write documentation only. You do not change source code,
tests, evals, workflows, or dependencies.

## Read first (and only these)
1. `AGENTS.md` (repository rules; they apply to the briefs you write).
2. `docs/agents/README.md`, `docs/agents/briefs/TEMPLATE.md`, and the finished example
   `docs/agents/briefs/23-structured-outputs.md`. Match its level of detail and tone.

## Target issues
Write one brief per issue, skipping any that already has `docs/agents/briefs/<n>-*.md`:
22, 24, 25, 26, 27, 28, 29, 30, 31 (AI-depth roadmap), then 9, 10, 12, 13, 14, 15, 16, 17, 18
(modernization follow-ups). Skip #11 (needs a human tester) and #32 and #23 (done).

For each issue:
1. Run `gh issue view <n> --json title,body` once and read it.
2. Locate the code it names with targeted searches (`rg -n "<symbol>"`), then read only the
   relevant line ranges. Do not read whole large files, `node_modules`, `.venv`, lock files,
   `data/`, `work/`, `outputs/`, `.env`, or anything secret.
3. Write `docs/agents/briefs/<n>-<slug>.md` from the template. Every entry under "Facts" must be
   verified against the code today (file path plus symbol, with an approximate line number). If you
   could not verify a claim, write `UNVERIFIED:` in front of it instead of guessing.
4. Fill in "Design decisions already made" with a concrete recommendation where the issue leaves a
   choice open (library, module location, limits), so the implementing agent does not deliberate.
   State the trade-off in one sentence.

## Sizing rules
- A brief must be completable in one agent session (roughly one focused pull request). If an issue
  is larger, split it into sub-briefs named `<n>a-<slug>.md`, `<n>b-<slug>.md`, ... each with its own
  outcome and acceptance, ordered so each is independently mergeable. #9 and #12 will almost
  certainly need this; #24, #26, #28 may.
- Put the intended tier (small, medium, strongest) and check mode (fast or `--full`) on the brief.
- Keep each brief under about 90 lines. Link to docs instead of copying them.

## Constraints every brief must carry (copy into "Do not touch" where relevant)
- `packages/` never imports `apps/`, FastAPI, or Streamlit (`tests/test_package_boundaries.py`).
- Telemetry and audit attributes are allowlists; never add a field that can carry a question, SQL,
  result rows, document text, prompts, or secrets.
- A change to a prompt, retrieval setting, fixture, or safety behavior updates `evals/v1/` and its
  baseline per `docs/governance/evaluation.md`.
- REST changes are additive and pass `python -m scripts.check_openapi_compatibility --base-ref origin/main`;
  regenerate the web client with `npm run generate:api`.
- The SQL guard stays SQLite-specific unless an ADR defines an equivalent.
- Provider and model selection is never request-selectable.
- Overlaps are not duplicated: OIDC is #9, durable persistence is #12, live-model evals are #14,
  the telemetry exporter is #15, streaming and idempotency are #10 and #17.

## Process rules (to keep this run cheap)
- Do not run the test suite, linters, Docker, or Playwright. Do not install anything.
- Do not paste large tool output back into your reasoning; use `rg -n` and short line ranges.
- Do not comment on, edit, or close any GitHub issue.

## Finish
1. Commit only the new files under `docs/agents/briefs/` on the current branch with the message
   `docs: add agent briefs for roadmap issues`. Do not push.
2. Reply with at most 12 lines: the briefs created (including splits), any issue you could not brief
   and why, and every `UNVERIFIED:` item that a human should check.
