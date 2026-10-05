from __future__ import annotations

import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import check_all, run_issue


def test_check_all_selects_fast_by_default_and_groups_on_request() -> None:
    checks = check_all.build_checks()

    fast = {c.name for c in check_all.select_checks(checks)}
    full = {c.name for c in check_all.select_checks(checks, full=True)}
    e2e = {c.name for c in check_all.select_checks(checks, full=True, e2e=True)}

    assert fast == {"ruff-check", "ruff-format", "mypy", "pytest", "evaluations"}
    assert fast < full and "openapi-compat" in full and "web-e2e" not in full
    assert "web-e2e" in e2e


def test_check_all_only_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="Unknown check"):
        check_all.select_checks(check_all.build_checks(), only=["nope"])


def test_run_check_reports_only_the_tail_of_a_failure() -> None:
    script = "import sys\nprint('\\n'.join(f'line {i}' for i in range(100)))\nsys.exit(3)"
    failing = check_all.Check("boom", (sys.executable, "-c", script))
    passing = check_all.Check("fine", (sys.executable, "-c", "print('quiet')"))

    passed, _, tail = check_all.run_check(failing, tail_lines=3)
    ok, _, ok_tail = check_all.run_check(passing)

    assert not passed and tail.splitlines() == ["line 97", "line 98", "line 99"]
    assert ok and ok_tail == ""


def test_run_check_handles_missing_executable() -> None:
    passed, _, tail = check_all.run_check(check_all.Check("x", ("no-such-binary-xyz",)))

    assert not passed and "Could not start" in tail


def test_main_returns_nonzero_and_names_failed_checks(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = [check_all.Check("bad", (sys.executable, "-c", "raise SystemExit(1)"))]
    monkeypatch.setattr(check_all, "build_checks", lambda base_ref="origin/main": fake)

    assert check_all.main([]) == 1
    assert "failed: ['bad']" in capsys.readouterr().out


def test_find_brief_and_branch(tmp_path: Path) -> None:
    (tmp_path / "7-example.md").write_text("x", encoding="utf-8")

    brief = run_issue.find_brief(7, tmp_path)

    assert run_issue.branch_for(brief) == "issue/7-example"
    with pytest.raises(run_issue.SetupError, match="No brief for issue #8"):
        run_issue.find_brief(8, tmp_path)


def test_split_briefs_require_an_explicit_selector(tmp_path: Path) -> None:
    first = tmp_path / "9a-auth-foundation.md"
    second = tmp_path / "9b-auth-provider.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    with pytest.raises(run_issue.SetupError, match="Choose one explicitly: 9a, 9b"):
        run_issue.find_brief("9", tmp_path)

    assert run_issue.find_brief("9a", tmp_path) == first
    assert run_issue.issue_number_for("9a") == 9
    assert run_issue.branch_for(first) == "issue/9a-auth-foundation"


def test_find_brief_rejects_ambiguous_and_invalid_selectors(tmp_path: Path) -> None:
    (tmp_path / "7-first.md").write_text("a", encoding="utf-8")
    (tmp_path / "7-second.md").write_text("b", encoding="utf-8")

    with pytest.raises(run_issue.SetupError, match="ambiguous"):
        run_issue.find_brief("7", tmp_path)
    with pytest.raises(run_issue.SetupError, match="issue number or a split"):
        run_issue.find_brief("../7", tmp_path)


def test_real_briefs_exist_for_documented_examples() -> None:
    assert run_issue.find_brief(23).name.startswith("23-")
    assert run_issue.find_brief(32).name.startswith("32-")


def test_brief_tier_selects_economical_defaults_and_allows_overrides() -> None:
    small = run_issue.find_brief(32)
    medium = run_issue.find_brief(23)

    assert run_issue.tier_for(small) == "small"
    assert run_issue.tier_for(medium) == "medium"
    assert run_issue.resolve_controls(
        "codex",
        "small",
        model=None,
        effort=None,
        budget_usd=None,
        timeout_minutes=None,
    ) == run_issue.RunControls("gpt-6-luna", "low", 2.0, 20)
    assert run_issue.resolve_controls(
        "codex",
        "strongest",
        model="custom-model",
        effort="high",
        budget_usd=3.0,
        timeout_minutes=10,
    ) == run_issue.RunControls("custom-model", "high", 3.0, 10)


def test_claude_uses_tier_effort_and_budget_without_forcing_a_model() -> None:
    assert run_issue.resolve_controls(
        "claude",
        "small",
        model=None,
        effort=None,
        budget_usd=None,
        timeout_minutes=None,
    ) == run_issue.RunControls(None, "low", 2.0, 20)


def test_controls_reject_non_positive_limits() -> None:
    with pytest.raises(run_issue.SetupError, match="greater than zero"):
        run_issue.resolve_controls(
            "codex",
            "small",
            model=None,
            effort=None,
            budget_usd=0,
            timeout_minutes=1,
        )
    with pytest.raises(run_issue.SetupError, match="greater than zero"):
        run_issue.resolve_controls(
            "codex",
            "small",
            model=None,
            effort=None,
            budget_usd=1,
            timeout_minutes=0,
        )


def test_build_command_for_claude_caps_spend_and_reads_prompt_from_stdin() -> None:
    command = run_issue.build_command(
        "claude",
        "claude",
        model="sonnet",
        effort="low",
        budget_usd=2.5,
        last_message_file=Path("unused"),
    )

    assert command[:2] == ["claude", "-p"]
    assert command[command.index("--max-budget-usd") + 1] == "2.5"
    assert command[command.index("--model") + 1] == "sonnet"
    assert "Bash(git *)" in command[command.index("--allowedTools") + 1]


def test_build_command_for_codex_is_sandboxed_and_reads_stdin() -> None:
    worktree = Path("work/agent-worktrees/23-structured-outputs")
    command = run_issue.build_command(
        "codex",
        "codex",
        model=None,
        effort="medium",
        budget_usd=None,
        last_message_file=Path("last.txt"),
        working_dir=worktree,
    )

    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[command.index("-C") + 1] == str(worktree)
    assert 'model_reasoning_effort="medium"' in command
    assert command[-1] == "-" and "-m" not in command


def test_build_command_rejects_unknown_agent() -> None:
    with pytest.raises(run_issue.SetupError):
        run_issue.build_command(
            "other", "x", model=None, effort=None, budget_usd=None, last_message_file=Path("x")
        )


def test_prompt_points_at_the_brief_and_forbids_pushing() -> None:
    brief = run_issue.find_brief(23)

    prompt = run_issue.build_prompt(23, brief, run_issue.branch_for(brief))

    assert "docs/agents/briefs/23-" in prompt
    assert "scripts.check_all" in prompt and "Do not push" in prompt


def test_validate_base_rejects_missing_workflow_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = run_issue.find_brief(23)

    def fake_git(*args: str) -> CompletedProcess[str]:
        returncode = 1 if args[:2] == ("cat-file", "-e") and "check_all.py" in args[2] else 0
        return CompletedProcess(["git", *args], returncode, "", "")

    monkeypatch.setattr(run_issue, "_git", fake_git)

    with pytest.raises(run_issue.SetupError, match="scripts/check_all.py"):
        run_issue.validate_base("main", brief)


def test_registered_worktrees_maps_branches_to_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    output = "\n".join(
        [
            "worktree C:/repo",
            "HEAD abc",
            "branch refs/heads/main",
            "",
            "worktree C:/repo/work/agent-worktrees/23-structured-outputs",
            "HEAD def",
            "branch refs/heads/issue/23-structured-outputs",
        ]
    )
    monkeypatch.setattr(
        run_issue,
        "_git",
        lambda *args: CompletedProcess(["git", *args], 0, output, ""),
    )

    worktrees = run_issue.registered_worktrees()

    assert worktrees["main"] == Path("C:/repo")
    assert worktrees["issue/23-structured-outputs"] == Path(
        "C:/repo/work/agent-worktrees/23-structured-outputs"
    )


def test_prepare_worktree_creates_a_linked_branch_without_switching_primary_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> CompletedProcess[str]:
        calls.append(args)
        if args == ("status", "--porcelain"):
            return CompletedProcess(["git", *args], 0, "", "")
        if args == ("worktree", "list", "--porcelain"):
            return CompletedProcess(
                ["git", *args], 0, "worktree C:/repo\nbranch refs/heads/main\n", ""
            )
        if args[:3] == ("rev-parse", "--verify", "--quiet"):
            return CompletedProcess(["git", *args], 1, "", "")
        return CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(run_issue, "_git", fake_git)
    destination = tmp_path / "23-structured-outputs"

    result = run_issue.prepare_worktree("issue/23-structured-outputs", "main", destination)

    assert result == destination
    assert (
        "worktree",
        "add",
        "-b",
        "issue/23-structured-outputs",
        str(destination),
        "main",
    ) in calls
    assert all("switch" not in call for call in calls)


def test_dry_run_prints_plan_without_touching_git(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_issue.main(["23", "--agent", "claude", "--base", "HEAD", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "branch: issue/23-" in out
    assert "worktree:" in out and "Claude USD cap" in out and "--max-budget-usd" in out


def test_codex_dry_run_describes_controls_without_claiming_a_spend_cap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_issue.main(["32", "--agent", "codex", "--base", "HEAD", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "no USD or token cap" in out
    assert "tier: small" in out and "effort=low" in out and "timeout=20m" in out
    assert "--max-budget-usd" not in out


def test_missing_brief_is_a_setup_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_issue.main(["99999", "--agent", "codex", "--dry-run"]) == 2
    assert "No brief for issue #99999" in capsys.readouterr().err
