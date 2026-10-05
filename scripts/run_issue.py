"""Run one roadmap issue headlessly with Claude Code or Codex, with bounded controls.

The agent receives a short prompt that points at a pre-written brief instead of exploring the
repository and the full issue text, which is the main source of wasted tokens::

    python -m scripts.run_issue 23 --agent claude --dry-run
    python -m scripts.run_issue 23 --agent claude --model sonnet --budget-usd 4
    python -m scripts.run_issue 23 --agent codex --model gpt-6-luna --effort medium
    python -m scripts.run_issue 9a --agent codex --model gpt-6-sol --effort medium

Briefs live in ``docs/agents/briefs/<selector>-<slug>.md``, where a selector is an issue number or
a split such as ``9a``. The run happens in a persistent linked worktree on branch
``issue/<selector>-<slug>``; nothing is pushed or committed. The agent performs at most two focused
checks, then this trusted runner executes the brief's complete check mode outside the model loop.
Full agent output goes to ``work/agent-runs/`` and only its tail is printed. Claude can receive a
USD cap. Codex is bounded by model, reasoning effort, prompt scope, and wall time; its JSON output
records token usage, but the CLI does not expose an equivalent spend cap here.

Exit status is the agent's, 124 for a timeout, 4 for failed trusted verification, 3 for an
incomplete agent result, or 2 for a setup problem.
"""

from __future__ import annotations

import argparse
import json
import os
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
BRIEF_CHECK_MODE = re.compile(r"Check mode:\s*(fast|--full)\b", re.IGNORECASE)
REQUIRED_BASE_PATHS = ("scripts/run_issue.py", "scripts/check_all.py")
CLAUDE_ALLOWED_TOOLS = ",".join(
    [
        "Bash(git diff *)",
        "Bash(git status *)",
        "Bash(python *)",
        "Bash(.venv/Scripts/python.exe *)",
        "Bash(npm *)",
    ]
)
PROMPT_TEMPLATE = """You are working on GitHub issue #{issue} in this repository (branch {branch}).

1. Read AGENTS.md and {brief} first. The brief is the scope. Do not re-read the full GitHub issue
   or explore beyond the files it lists unless a listed fact turns out to be wrong.
2. Make the smallest change that meets the brief's acceptance criteria. Respect its
   "Do not touch" list.
3. Run at most two narrowly targeted verification commands for the files you changed. Do not run
   `scripts.check_all`, the full pytest suite, Docker, or Playwright: the trusted runner performs
   the complete check after you exit. Do not retry the same failed command more than once.
4. If you change a prompt, retrieval setting, fixture, or safety behavior, update evals/v1 and its
   baseline as described in docs/governance/evaluation.md.
5. Leave the changes uncommitted. Do not use Git to commit, push, open a pull request, or comment
   on or close the issue; a trusted step reviews and commits after verification.
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
    "small": RunControls("gpt-6-luna", "low", 2.0, 10),
    "medium": RunControls("gpt-6-luna", "medium", 5.0, 15),
    "strongest": RunControls("gpt-6-sol", "medium", 8.0, 30),
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


def check_mode_for(brief: Path) -> str:
    match = BRIEF_CHECK_MODE.search(brief.read_text(encoding="utf-8"))
    if match is None:
        raise SetupError(
            f"Brief does not declare `Check mode: fast` or `Check mode: --full`: {brief}"
        )
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
            "--ephemeral",
            "--json",
            "-s",
            "workspace-write",
            "-C",
            str(working_dir),
            "-o",
            str(last_message_file),
        ]
        if model:
            command += ["-m", model]
        if effort:
            command += ["-c", f'model_reasoning_effort="{effort}"']
        return [*command, "-"]
    raise SetupError(f"Unknown agent: {agent}")


def available_codex_models(executable: str) -> tuple[str, ...]:
    """Read the signed-in account's catalog without starting a metered agent turn."""
    completed = subprocess.run(
        [executable, "debug", "models"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f": {detail}" if detail else "."
        raise SetupError(f"Could not read the Codex model catalog{suffix}")
    try:
        payload = json.loads(completed.stdout)
        models = payload["models"]
        slugs = tuple(
            model["slug"]
            for model in models
            if isinstance(model, dict) and isinstance(model.get("slug"), str)
        )
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise SetupError("Codex returned an invalid model catalog.") from error
    if not slugs:
        raise SetupError("Codex returned an empty model catalog.")
    return slugs


def validate_codex_model(executable: str, model: str) -> None:
    """Fail before inference when a selected model is unavailable to this account."""
    available = available_codex_models(executable)
    if model in available:
        return
    choices = ", ".join(available)
    raise SetupError(
        f"Codex model `{model}` is not available to the signed-in account. "
        f"Available models: {choices}. Choose one with --model or update the tier default."
    )


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


def validate_agent_result(path: Path, initial_head: str) -> None:
    """Accept uncommitted edits (preferred) or a commit, but reject a no-op success."""
    if _worktree_is_clean(path) and worktree_head(path) == initial_head:
        raise SetupError(
            "Agent exited successfully but made no changes. Review its log; the run is incomplete."
        )


def verification_command(check_mode: str) -> list[str]:
    command = [sys.executable, "-m", "scripts.check_all"]
    if check_mode == "--full":
        command.append("--full")
    return command


def verification_environment(worktree: Path) -> dict[str, str]:
    """Keep test/process scratch data inside the isolated, git-ignored worktree."""
    temp_dir = worktree / "work" / "agent-verification-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({"TEMP": str(temp_dir), "TMP": str(temp_dir), "TMPDIR": str(temp_dir)})
    return environment


def codex_token_usage(output: str) -> dict[str, int] | None:
    """Extract the final usage counters from Codex JSONL without depending on other events."""
    for line in reversed(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        usage = event["usage"]
        keys = ("input_tokens", "cached_input_tokens", "output_tokens")
        return {key: int(usage.get(key, 0)) for key in keys}
    return None


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
        check_mode = check_mode_for(brief)
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
        if args.agent == "codex" and not args.dry_run and controls.model is not None:
            validate_codex_model(executable or args.agent, controls.model)
        log_file = LOG_DIR / f"{brief.stem}-{args.agent}.log"
        last_message_file = LOG_DIR / f"{brief.stem}-{args.agent}.last.txt"
        command = build_command(
            args.agent,
            executable or args.agent,
            model=controls.model,
            effort=controls.effort,
            budget_usd=controls.budget_usd,
            last_message_file=last_message_file,
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
                f"trusted check: {' '.join(verification_command(check_mode))}\n"
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
                last_message_file=last_message_file,
                working_dir=active_worktree,
            )
        initial_head = worktree_head(active_worktree)
    except SetupError as error:
        print(error, file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    last_message_file.unlink(missing_ok=True)
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
    if args.agent == "codex" and last_message_file.exists():
        visible_output = last_message_file.read_text(encoding="utf-8", errors="replace")
    else:
        visible_output = output
    print("\n".join(visible_output.strip().splitlines()[-args.tail :]))
    usage = codex_token_usage(output) if args.agent == "codex" else None
    if usage is not None:
        print(
            "usage: "
            f"input={usage['input_tokens']} cached_input={usage['cached_input_tokens']} "
            f"output={usage['output_tokens']}"
        )
    returncode = completed.returncode
    if returncode == 0:
        try:
            validate_agent_result(active_worktree, initial_head)
        except SetupError as error:
            print(f"\n{error}", file=sys.stderr)
            returncode = 3
    if returncode == 0:
        check = subprocess.run(
            verification_command(check_mode),
            cwd=active_worktree,
            env=verification_environment(active_worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        check_output = check.stdout + check.stderr
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write("\n\n--- trusted verification ---\n")
            stream.write(check_output)
        print("\ntrusted verification:")
        print("\n".join(check_output.strip().splitlines()[-args.tail :]))
        if check.returncode != 0:
            returncode = 4
    print(f"\nexit={returncode} branch={branch} worktree={active_worktree} log={log_file}")
    return returncode


if __name__ == "__main__":
    sys.exit(main())
