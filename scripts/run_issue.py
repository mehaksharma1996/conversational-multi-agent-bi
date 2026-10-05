"""Run one roadmap issue headlessly with Claude Code or Codex, with spend and scope caps.

The agent receives a short prompt that points at a pre-written brief instead of exploring the
repository and the full issue text, which is the main source of wasted tokens::

    python -m scripts.run_issue 23 --agent claude --dry-run
    python -m scripts.run_issue 23 --agent claude --model sonnet --budget-usd 4
    python -m scripts.run_issue 23 --agent codex --effort medium

Briefs live in ``docs/agents/briefs/<issue>-<slug>.md``. The run happens on branch
``issue/<issue>-<slug>``; nothing is pushed. Full agent output goes to ``work/agent-runs/`` and
only its tail is printed. Exit status is the agent's, or 2 for a setup problem.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEFS_DIR = REPO_ROOT / "docs" / "agents" / "briefs"
LOG_DIR = REPO_ROOT / "work" / "agent-runs"
CLAUDE_ALLOWED_TOOLS = ",".join(
    [
        "Bash(git *)",
        "Bash(python *)",
        "Bash(.venv/Scripts/python.exe *)",
        "Bash(npm *)",
        "Bash(gh issue view *)",
    ]
)
PROMPT_TEMPLATE = """You are working on GitHub issue #{issue} in this repository (branch {branch}).

1. Read AGENTS.md and {brief} first. The brief is the scope. Do not re-read the full GitHub issue
   or explore beyond the files it lists unless a listed fact turns out to be wrong.
2. Make the smallest change that meets the brief's acceptance criteria. Respect its
   "Do not touch" list.
3. Verify with `python -m scripts.check_all` (add `--full` if you changed the API, OpenAPI, or
   apps/web). Rerun a single failing check with `--only <name>`. Never paste full tool logs.
4. If you change a prompt, retrieval setting, fixture, or safety behavior, update evals/v1 and its
   baseline as described in docs/governance/evaluation.md.
5. Commit on this branch with a conventional message. Do not push, open a pull request, or
   comment on or close the issue.
6. Finish with at most 10 lines: what changed, check results, open risks.
"""


class SetupError(RuntimeError):
    """The run cannot start (missing brief, dirty tree, missing CLI)."""


def find_brief(issue: int, briefs_dir: Path = BRIEFS_DIR) -> Path:
    matches = sorted(briefs_dir.glob(f"{issue}-*.md"))
    if not matches:
        raise SetupError(
            f"No brief for issue #{issue} in {briefs_dir}. "
            "Write one from TEMPLATE.md (see docs/agents/README.md) first."
        )
    return matches[0]


def branch_for(brief: Path) -> str:
    return f"issue/{brief.stem}"


def build_prompt(issue: int, brief: Path, branch: str) -> str:
    relative = brief.relative_to(REPO_ROOT).as_posix() if brief.is_absolute() else brief.as_posix()
    return PROMPT_TEMPLATE.format(issue=issue, branch=branch, brief=relative)


def build_command(
    agent: str,
    executable: str,
    *,
    model: str | None,
    effort: str | None,
    budget_usd: float | None,
    last_message_file: Path,
) -> list[str]:
    """Return the headless command. The prompt is sent on stdin to avoid shell quoting limits."""
    if agent == "claude":
        command = [
            executable,
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            CLAUDE_ALLOWED_TOOLS,
        ]
        if model:
            command += ["--model", model]
        if effort:
            command += ["--effort", effort]
        if budget_usd is not None:
            command += ["--max-budget-usd", str(budget_usd)]
        return command
    if agent == "codex":
        command = [
            executable,
            "exec",
            "-s",
            "workspace-write",
            "-C",
            str(REPO_ROOT),
            "-o",
            str(last_message_file),
        ]
        if model:
            command += ["-m", model]
        if effort:
            command += ["-c", f'model_reasoning_effort="{effort}"']
        return [*command, "-"]
    raise SetupError(f"Unknown agent: {agent}")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8"
    )


def prepare_branch(branch: str, base: str) -> None:
    """Require a clean tree, then create (or resume) the issue branch."""
    if _git("status", "--porcelain").stdout.strip():
        raise SetupError("Working tree is not clean; commit or stash before running an agent.")
    exists = _git("rev-parse", "--verify", "--quiet", branch).returncode == 0
    result = _git("switch", branch) if exists else _git("switch", "-c", branch, base)
    if result.returncode != 0:
        raise SetupError(f"Could not switch to {branch}: {result.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("issue", type=int, help="GitHub issue number (needs a brief).")
    parser.add_argument("--agent", choices=["claude", "codex"], required=True)
    parser.add_argument("--model", help="Model alias or id. Default: the CLI's configured model.")
    parser.add_argument("--effort", default="medium", help="Reasoning effort (default: medium).")
    parser.add_argument("--budget-usd", type=float, default=5.0, help="Claude spend cap.")
    parser.add_argument("--base", default="main", help="Base ref for a new branch.")
    parser.add_argument("--timeout-minutes", type=int, default=60)
    parser.add_argument("--tail", type=int, default=15, help="Output lines to print.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; run nothing.")
    args = parser.parse_args(argv)

    try:
        brief = find_brief(args.issue)
        branch = branch_for(brief)
        executable = shutil.which(args.agent)
        if executable is None and not args.dry_run:
            raise SetupError(f"`{args.agent}` was not found on PATH.")
        log_file = LOG_DIR / f"{brief.stem}-{args.agent}.log"
        command = build_command(
            args.agent,
            executable or args.agent,
            model=args.model,
            effort=args.effort,
            budget_usd=args.budget_usd,
            last_message_file=LOG_DIR / f"{brief.stem}-{args.agent}.last.txt",
        )
        prompt = build_prompt(args.issue, brief, branch)
        if args.dry_run:
            print(f"branch: {branch}\nlog: {log_file}\ncommand: {' '.join(command)}\n---\n{prompt}")
            return 0
        prepare_branch(branch, args.base)
    except SetupError as error:
        print(error, file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout_minutes * 60,
        )
    except subprocess.TimeoutExpired:
        print(f"Timed out after {args.timeout_minutes} minutes on {branch}.", file=sys.stderr)
        return 124
    output = completed.stdout + completed.stderr
    log_file.write_text(output, encoding="utf-8")
    print("\n".join(output.strip().splitlines()[-args.tail :]))
    print(f"\nexit={completed.returncode} branch={branch} log={log_file}")
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
