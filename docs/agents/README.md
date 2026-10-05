# Working on roadmap issues with AI agents (Claude Code and Codex)

Goal: finish each issue in one short, bounded agent session. Most wasted usage comes from
(1) full tool logs read back into context, (2) re-exploring the repository and re-reading long
issue text, and (3) long sessions that re-send everything every turn. This folder removes all three.

## The loop

1. **Brief.** Each issue has `docs/agents/briefs/<issue>-<slug>.md`, written from
   [`TEMPLATE.md`](briefs/TEMPLATE.md): scope, exact files, "do not touch", acceptance checks. The
   agent reads the brief instead of exploring. Missing briefs can be generated with
   [`prompts/generate-briefs.md`](prompts/generate-briefs.md) (a Codex prompt; one cheap batch).
2. **Run.** One fresh session per issue, on its own branch, with a spend cap:

   ```powershell
   python -m scripts.run_issue 23 --agent claude --dry-run     # inspect the plan and prompt
   python -m scripts.run_issue 23 --agent claude --model sonnet --budget-usd 4
   python -m scripts.run_issue 23 --agent codex --effort medium
   ```

   The runner requires a clean tree, switches to `issue/<n>-<slug>`, sends the prompt on stdin,
   logs everything to `work/agent-runs/` (git-ignored), and prints only the tail. Nothing is pushed.
3. **Verify quietly.** `python -m scripts.check_all` prints one PASS/FAIL line per check and the
   last 25 lines of a failing check only. `--only pytest` reruns one check, `--full` adds the
   OpenAPI gate and web checks, `--e2e` adds Playwright. CI still runs the complete matrix, so do
   not run Docker or Playwright inside an agent loop.
4. **Review and push yourself.** Read the diff, push, open the PR, and read CI with
   `gh run view <id> --log-failed | tail -40` rather than the whole log.

## Rules that save usage

- One issue per session. Commit, then start fresh. Never "continue" a finished issue.
- Pick the model by the work. Mechanical or documentation issues: a smaller model and `--effort low`
  or `medium`. Design-heavy issues (#24 LangGraph interrupt, #22 MCP, #26 retrieval, #28 Postgres):
  the strongest model, with the brief's design decisions made up front.
- Split issues that are too large for one session (#9, #12) into sub-briefs (`9a-...`, `9b-...`);
  the runner takes the first brief matching `<issue>-*`.
- Keep this guidance short. `AGENTS.md` is read at the start of every session by both tools.
- Do not ask an agent to "explore" or "understand the project" again. Update the brief instead.

## Suggested order and model tier

| Order | Issue | Tier |
|---|---|---|
| 1 | #32 Non-goals ADR | small model, docs only |
| 2 | #23 Structured outputs | medium |
| 3 | #24 LangGraph loops and SQL approval | strongest |
| 4 | #22 MCP server | strongest |
| 5 | #25 LLM telemetry | medium |
| 6 | #26 Retrieval upgrade | strongest |
| 7 | #27 Second provider | medium to strong |
| 8 | #29 Trajectory evals | medium |
| 9 | #28 Postgres and pgvector | strongest |
| 10 | #30 Supervised classifier | medium |
| 11 | #31 Rate limiting, caching, benchmarks | medium |

The #6 follow-ups (#9 to #18) are larger and should be split into briefs before a session.
