from __future__ import annotations

import sys
from pathlib import Path

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


def test_real_briefs_exist_for_documented_examples() -> None:
    assert run_issue.find_brief(23).name.startswith("23-")
    assert run_issue.find_brief(32).name.startswith("32-")


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
    command = run_issue.build_command(
        "codex",
        "codex",
        model=None,
        effort="medium",
        budget_usd=None,
        last_message_file=Path("last.txt"),
    )

    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-s") + 1] == "workspace-write"
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


def test_dry_run_prints_plan_without_touching_git(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_issue.main(["23", "--agent", "claude", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "branch: issue/23-" in out and "--max-budget-usd" in out


def test_missing_brief_is_a_setup_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_issue.main(["99999", "--agent", "codex", "--dry-run"]) == 2
    assert "No brief for issue #99999" in capsys.readouterr().err
