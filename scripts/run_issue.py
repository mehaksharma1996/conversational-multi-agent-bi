"""Run one roadmap issue headlessly with Claude Code or Codex, with bounded controls.

The agent receives a short prompt that points at a pre-written brief instead of exploring the
repository and the full issue text, which is the main source of wasted tokens::

    python -m scripts.run_issue 23 --agent claude --dry-run
    python -m scripts.run_issue 23 --agent claude --model sonnet --budget-usd 4
    python -m scripts.run_issue 23 --agent codex --model gpt-6-luna --effort medium
    python -m scripts.run_issue 9a --agent codex --model gpt-6.1-sol --effort medium

Briefs live in ``docs/agents/briefs/<selector>-<slug>.md``, where a selector is an issue number or
a split such as ``9a``. The run happens in a persistent linked worktree on branch
``issue/<selector>-<slug>``; nothing is pushed. Full agent output goes to ``work/agent-runs/`` and
only its tail is printed. Claude can receive a USD cap. Codex is bounded by model, reasoning
effort, prompt scope, and wall time; the CLI does not expose an equivalent spend cap here.

Exit status is the agent's, 124 for a timeout, 3 for an incomplete agent result, or 2 for a setup
problem.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIEFS_DIR = REPO_ROOT / "docs" / "agents" / "briefs"
LOG_DIR = REPO_ROOT / "work" / "agent-runs"
WORKTREES_DIR = REPO_ROOT / "work" / "agent-worktrees"
BRIEF_SELECTOR = re.compile(r"(?P<issue>[1-9][0-9]*)(?P<part>[a-z]?)\Z")
BRIEF_TIER = re.compile(r"^Tier:\s*(small|medium|strongest)\b", re.IGNORECASE | re.MULTILINE)
REQUIRED_BASE_PATHS = ("scripts/run_issue.py", "scripts/check_all.py")
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


@dataclass(frozen=True)
class RunControls:
    model: str | None
    effort: str
    budget_usd: float
    timeout_minutes: int


TIER_DEFAULTS = {
    "small": RunControls("gpt-6-luna", "low", 2.0, 20),
    "medium": RunControls("gpt-6-luna", "medium", 5.0, 45),
    "strongest": RunControls("gpt-6.1-sol", "medium", 8.0, 60),
}


def normalize_selector(selector: str | int) -> str:
    value = str(selector).strip().lower()
    if not BRIEF_SELECTOR.fullmatch(value):
        raise SetupError("Brief selector must be an issue number or a split such as `9a`.")
    return value


def issue_number_for(selector: str | int) -> int:
    match = BRIEF_SELECTOR.fullmatch(normalize_selector(selector))
    assert match is not None
    return int(match.group("issue"))


def find_brief(selector: str | int, briefs_dir: Path = BRIEFS_DIR) -> Path:
    key = normalize_selector(selector)
    matches = sorted(briefs_dir.glob(f"{key}-*.md"))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise SetupError(f"Brief selector `{key}` is ambiguous: {names}")

    if key.isdigit():
        split_matches = sorted(briefs_dir.glob(f"{key}[a-z]-*.md"))
        if split_matches:
            selectors = ", ".join(path.name.split("-", 1)[0] for path in split_matches)
            raise SetupError(f"Issue #{key} has split briefs. Choose one explicitly: {selectors}.")

    label = f"issue #{key}" if key.isdigit() else f"brief `{key}`"
    raise SetupError(
        f"No brief for {label} in {briefs_dir}. "
        "Write one from TEMPLATE.md (see docs/agents/README.md) first."
    )


def branch_for(brief: Path) -> str:
    return f"issue/{brief.stem}"


def worktree_for(brief: Path) -> Path:
    return WORKTREES_DIR / brief.stem


def tier_for(brief: Path) -> str:
    match = BRIEF_TIER.search(brief.read_text(encoding="utf-8"))
    if match is None:
        raise SetupError(f"Brief does not declare `Tier: small`, `medium`, or `strongest`: {brief}")
    return match.group(1).lower()


def resolve_controls(
    agent: str,
    tier: str,
    *,
    model: str | None,
    effort: str | None,
    budget_usd: float | None,
    timeout_minutes: int | None,
) -> RunControls:
    defaults = TIER_DEFAULTS[tier]
    selected_model = model if model is not None else (defaults.model if agent == "codex" else None)
    selected_effort = effort or defaults.effort
    selected_budget = defaults.budget_usd if budget_usd is None else budget_usd
    selected_timeout = defaults.timeout_minutes if timeout_minutes is None else timeout_minutes
    if selected_budget <= 0:
        raise SetupError("--budget-usd must be greater than zero.")
    if selected_timeout <= 0:
        raise SetupError("--timeout-minutes must be greater than zero.")
    return RunControls(selected_model, selected_effort, selected_budget, selected_timeout)


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
    working_dir: Path = REPO_ROOT,
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
            str(working_dir),
            "--add-dir",
            str(REPO_ROOT / ".git"),
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
        ["git", "-c", f"safe.directory={REPO_ROOT.as_posix()}", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def validate_base(base: str, brief: Path) -> None:
    """Require a real commit containing the runner, quiet checks, and selected brief."""
    commit = _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if commit.returncode != 0:
        raise SetupError(f"Base ref `{base}` is missing or is not a commit.")

    brief_relative = brief.relative_to(REPO_ROOT).as_posix()
    missing = [
        path
        for path in (*REQUIRED_BASE_PATHS, brief_relative)
        if _git("cat-file", "-e", f"{base}:{path}").returncode != 0
    ]
    if missing:
        paths = ", ".join(missing)
        raise SetupError(
            f"Base `{base}` does not contain the issue workflow files: {paths}. "
            "Merge the workflow, update the base branch, or pass a base that contains them."
        )


def registered_worktrees() -> dict[str, Path]:
    """Return local branch name to linked-worktree path from porcelain Git output."""
    result = _git("worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise SetupError(f"Could not list Git worktrees: {result.stderr.strip()}")

    worktrees: dict[str, Path] = {}
    current_path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/") and current_path is not None:
            branch = line.removeprefix("branch refs/heads/")
            worktrees[branch] = current_path
    return worktrees


def _worktree_is_clean(path: Path) -> bool:
    result = _git_in_worktree(path, "status", "--porcelain")
    if result.returncode != 0:
        raise SetupError(f"Could not inspect worktree {path}: {result.stderr.strip()}")
    return not result.stdout.strip()


def _git_in_worktree(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={path.as_posix()}", *args],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def worktree_head(path: Path) -> str:
    result = _git_in_worktree(path, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise SetupError(f"Could not read HEAD in {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_agent_commit(path: Path, initial_head: str) -> None:
    """Reject a nominally successful run that did not leave one or more clean commits."""
    if not _worktree_is_clean(path):
        raise SetupError(
            "Agent exited successfully but left uncommitted changes. Review the worktree; "
            "the run is incomplete."
        )
    if worktree_head(path) == initial_head:
        raise SetupError(
            "Agent exited successfully but created no commit. Review the worktree; "
            "the run is incomplete."
        )


def prepare_worktree(branch: str, base: str, destination: Path) -> Path:
    """Require clean state, then create or safely resume a persistent linked worktree."""
    if _git("status", "--porcelain").stdout.strip():
        raise SetupError("Working tree is not clean; commit or stash before running an agent.")

    registered = registered_worktrees()
    if branch in registered:
        path = registered[branch]
        if path.resolve() == REPO_ROOT.resolve():
            raise SetupError(
                f"Branch `{branch}` is checked out in the primary workspace. "
                "Switch the primary workspace to the base branch before resuming it."
            )
        if not _worktree_is_clean(path):
            raise SetupError(f"Issue worktree is not clean: {path}")
        return path

    if destination.exists():
        raise SetupError(
            f"Worktree destination already exists but is not registered with Git: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    exists = _git("rev-parse", "--verify", "--quiet", branch).returncode == 0
    args = (
        ("worktree", "add", str(destination), branch)
        if exists
        else (
            "worktree",
            "add",
            "-b",
            branch,
            str(destination),
            base,
        )
    )
    result = _git(*args)
    if result.returncode != 0:
        raise SetupError(f"Could not create worktree for {branch}: {result.stderr.strip()}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("brief", help="Issue number or split brief selector such as 9a.")
    parser.add_argument("--agent", choices=["claude", "codex"], required=True)
    parser.add_argument("--model", help="Model alias or id. Default: selected from the brief tier.")
    parser.add_argument("--effort", help="Reasoning effort. Default: selected from the brief tier.")
    parser.add_argument(
        "--budget-usd", type=float, help="Claude-only spend cap. Default: selected by brief tier."
    )
    parser.add_argument("--base", default="main", help="Base ref for a new branch.")
    parser.add_argument(
        "--timeout-minutes", type=int, help="Wall-time cap selected by tier by default."
    )
    parser.add_argument("--tail", type=int, default=15, help="Output lines to print.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan; run nothing.")
    args = parser.parse_args(argv)

    try:
        brief = find_brief(args.brief)
        issue = issue_number_for(args.brief)
        tier = tier_for(brief)
        controls = resolve_controls(
            args.agent,
            tier,
            model=args.model,
            effort=args.effort,
            budget_usd=args.budget_usd,
            timeout_minutes=args.timeout_minutes,
        )
        branch = branch_for(brief)
        planned_worktree = worktree_for(brief)
        validate_base(args.base, brief)
        executable = shutil.which(args.agent)
        if executable is None and not args.dry_run:
            raise SetupError(f"`{args.agent}` was not found on PATH.")
        log_file = LOG_DIR / f"{brief.stem}-{args.agent}.log"
        command = build_command(
            args.agent,
            executable or args.agent,
            model=controls.model,
            effort=controls.effort,
            budget_usd=controls.budget_usd,
            last_message_file=LOG_DIR / f"{brief.stem}-{args.agent}.last.txt",
            working_dir=planned_worktree,
        )
        prompt = build_prompt(issue, brief, branch)
        if args.dry_run:
            control = (
                f"Claude USD cap: ${controls.budget_usd:g}"
                if args.agent == "claude"
                else "Codex controls: model/effort/scope/wall-time (no USD or token cap)"
            )
            print(
                f"branch: {branch}\nworktree: {planned_worktree}\nbase: {args.base}\ntier: {tier}\n"
                f"controls: {control}; model={controls.model or 'CLI default'}; "
                f"effort={controls.effort}; timeout={controls.timeout_minutes}m\n"
                f"log: {log_file}\ncommand: {' '.join(command)}\n---\n{prompt}"
            )
            return 0
        active_worktree = prepare_worktree(branch, args.base, planned_worktree)
        if active_worktree != planned_worktree:
            command = build_command(
                args.agent,
                executable or args.agent,
                model=controls.model,
                effort=controls.effort,
                budget_usd=controls.budget_usd,
                last_message_file=LOG_DIR / f"{brief.stem}-{args.agent}.last.txt",
                working_dir=active_worktree,
            )
        initial_head = worktree_head(active_worktree)
    except SetupError as error:
        print(error, file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=active_worktree,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=controls.timeout_minutes * 60,
        )
    except subprocess.TimeoutExpired:
        print(f"Timed out after {controls.timeout_minutes} minutes on {branch}.", file=sys.stderr)
        return 124
    output = completed.stdout + completed.stderr
    log_file.write_text(output, encoding="utf-8")
    print("\n".join(output.strip().splitlines()[-args.tail :]))
    returncode = completed.returncode
    if returncode == 0:
        try:
            validate_agent_commit(active_worktree, initial_head)
        except SetupError as error:
            print(f"\n{error}", file=sys.stderr)
            returncode = 3
    print(f"\nexit={returncode} branch={branch} worktree={active_worktree} log={log_file}")
    return returncode


if __name__ == "__main__":
    sys.exit(main())
