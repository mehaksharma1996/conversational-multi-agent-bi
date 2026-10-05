# Working on roadmap issues with AI agents (Claude Code and Codex)

Goal: finish each issue in one short, bounded agent session. Most wasted usage comes from
(1) full tool logs read back into context, (2) re-exploring the repository and re-reading long
issue text, and (3) long sessions that re-send everything every turn. This folder removes all three.

## The loop

1. **Brief.** Each issue or independently mergeable split has
   `docs/agents/briefs/<selector>-<slug>.md`, written from
   [`TEMPLATE.md`](briefs/TEMPLATE.md): scope, exact files, "do not touch", acceptance checks. The
   agent reads the brief instead of exploring. Missing briefs can be generated with
   [`prompts/generate-briefs.md`](prompts/generate-briefs.md) (a Codex prompt; one cheap batch).
2. **Run.** One fresh session per issue, in its own persistent linked worktree, with bounded
   controls:

   ```powershell
   python -m scripts.run_issue 23 --agent claude --dry-run     # inspect the plan and prompt
   python -m scripts.run_issue 23 --agent claude --model sonnet --budget-usd 4
   python -m scripts.run_issue 23 --agent codex             # model/effort come from brief tier
   ```

   Merge this workflow first, then update the local base before the first live run:

   ```powershell
   git switch main
   git pull --ff-only
   python -m scripts.run_issue 32 --agent codex --model gpt-6-luna --effort low --dry-run
   ```

   The runner verifies that the selected base contains the runner, quiet checks, and chosen brief.
   It requires a clean primary checkout, creates or resumes
   `work/agent-worktrees/<selector>-<slug>` on `issue/<selector>-<slug>`, sends the prompt on stdin,
   logs everything to `work/agent-runs/` (git-ignored), and prints only the tail. The primary
   checkout stays on its current branch and nothing is pushed.

   Linked worktrees do not inherit ignored dependencies. Keep the Python environment activated;
   for a frontend brief, run `npm ci` once from `<worktree>\apps\web` before the live agent run.
   The runner never installs dependencies automatically.

   `--budget-usd` is a Claude-only hard spend cap. Codex CLI does not expose an equivalent USD or
   token cap through this runner; Codex usage is controlled with the selected model, reasoning
   effort, narrow brief, concise check output, and `--timeout-minutes` wall-time limit.

   Unless overridden on the command line, the brief's `Tier:` selects these controls:

   | Tier | Codex model | Effort | Claude budget | Wall time |
   |---|---|---|---:|---:|
   | `small` | `gpt-6-luna` | low | $2 | 20 minutes |
   | `medium` | `gpt-6-luna` | medium | $5 | 45 minutes |
   | `strongest` | `gpt-6.1-sol` | medium | $8 | 60 minutes |

   Claude keeps its configured model unless `--model` is supplied. Codex never selects Astra
   automatically. Use `--model`, `--effort`, `--budget-usd`, or `--timeout-minutes` for a deliberate
   per-run override; the dry run prints the resolved values before spending anything.
3. **Verify quietly.** `python -m scripts.check_all` prints one PASS/FAIL line per check and the
   last 25 lines of a failing check only. `--only pytest` reruns one check, `--full` adds the
   OpenAPI gate and web checks, `--e2e` adds Playwright. CI still runs the complete matrix, so do
   not run Docker or Playwright inside an agent loop.
4. **Review and push yourself.** Read the diff, push, open the PR, and read CI with
   `gh run view <id> --log-failed | tail -40` rather than the whole log.

   The final line printed by the runner includes the exact worktree path. Run review and push
   commands there, or use `git -C <worktree-path> ...` from the primary checkout.

## Rules that save usage

- One issue per session. Commit, then start fresh. Never "continue" a finished issue.
- Pick the model by the work. Mechanical or documentation issues: a smaller model and `--effort low`
  or `medium`. Design-heavy issues (#24 LangGraph interrupt, #22 MCP, #26 retrieval, #28 Postgres):
  the strongest model, with the brief's design decisions made up front.
- Split issues that are too large for one session (#9, #12) into sub-briefs (`9a-...`, `9b-...`).
  Select the part explicitly, for example `python -m scripts.run_issue 9a --agent codex ...`.
  Supplying only `9` fails safely and lists the available parts; the runner never silently chooses
  one of several briefs.
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
