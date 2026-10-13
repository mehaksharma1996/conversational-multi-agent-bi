"""Keep Claude Code skill copies in sync with the canonical Codex skills, and detect drift.

Codex reads project skills from ``.agents/skills/`` and Claude Code only from
``.claude/skills/``. ``.agents/skills/`` is canonical; edit skills there, then::

    python -m scripts.sync_agent_skills                    # copy canonical -> .claude/skills
    python -m scripts.sync_agent_skills --check            # exit 1 on any drift or violation
    python -m scripts.sync_agent_skills --update-manifest  # record local file hashes

Plain file copies are used instead of symlinks, which are fragile on Windows. Hashes are taken
over LF-normalised text so a CRLF checkout does not look like drift. Only skills listed in
``docs/agents/toolkit-manifest.json`` are managed; other local skills are never touched.

``--check`` also enforces the toolkit's invocation policy: a skill whose manifest entry says
``"invocation": "explicit"`` must set ``disable-model-invocation: true`` (Claude Code) and
``allow_implicit_invocation: false`` (Codex), so no skill can start from an ordinary prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL = Path(".agents/skills")
MIRROR = Path(".claude/skills")
MANIFEST = Path("docs/agents/toolkit-manifest.json")
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".txt", ".py"}
IGNORED_PARTS = {"__pycache__"}
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def _digest(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def tree_hashes(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): _digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not IGNORED_PARTS & set(path.parts)
    }


def load_manifest(root: Path) -> dict[str, object]:
    return json.loads((root / MANIFEST).read_text(encoding="utf-8"))


def _skills(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    skills = manifest.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("manifest has no 'skills' object")
    return skills


def frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER.match(text)
    if match is None:
        return {}
    pairs = (line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    return {key.strip(): value.strip().strip("\"'") for key, value in pairs}


def sync(root: Path) -> list[str]:
    """Copy each managed canonical skill to the Claude mirror. Returns the actions taken."""
    actions: list[str] = []
    for name in _skills(load_manifest(root)):
        source, target = root / CANONICAL / name, root / MIRROR / name
        if not source.is_dir():
            raise FileNotFoundError(f"canonical skill missing: {CANONICAL / name}")
        wanted = tree_hashes(source)
        have = tree_hashes(target)
        for relative in sorted(set(have) - set(wanted)):
            (target / relative).unlink()
            actions.append(f"removed {MIRROR / name / relative}")
        for relative, digest in wanted.items():
            if have.get(relative) != digest:
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target / relative)
                actions.append(f"copied {MIRROR / name / relative}")
    return actions


def check(root: Path) -> list[str]:
    problems: list[str] = []
    manifest = load_manifest(root)
    for name, entry in _skills(manifest).items():
        source, target = root / CANONICAL / name, root / MIRROR / name
        wanted, have = tree_hashes(source), tree_hashes(target)
        if not wanted:
            problems.append(f"{name}: canonical skill missing or empty")
            continue
        for relative in sorted(set(wanted) | set(have)):
            if relative not in have:
                problems.append(f"{name}: missing in {MIRROR}: {relative}")
            elif relative not in wanted:
                problems.append(f"{name}: extra in {MIRROR}: {relative}")
            elif wanted[relative] != have[relative]:
                problems.append(f"{name}: drift: {relative}")
        recorded = entry.get("local_files")
        if recorded != wanted:
            problems.append(f"{name}: manifest local_files stale (run --update-manifest)")
        problems += _policy_problems(name, entry, source)
    return problems


def _policy_problems(name: str, entry: dict[str, object], source: Path) -> list[str]:
    problems: list[str] = []
    skill_md = source / "SKILL.md"
    if not skill_md.is_file():
        return [f"{name}: SKILL.md missing"]
    meta = frontmatter(skill_md.read_text(encoding="utf-8"))
    if meta.get("name") != name:
        problems.append(f"{name}: frontmatter name {meta.get('name')!r} != directory")
    if not meta.get("description"):
        problems.append(f"{name}: frontmatter description missing")
    if entry.get("invocation") == "explicit":
        if meta.get("disable-model-invocation") != "true":
            problems.append(f"{name}: explicit-only skill lacks disable-model-invocation: true")
        yaml_path = source / "agents" / "openai.yaml"
        yaml_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
        if not re.search(r"(?m)^\s*allow_implicit_invocation:\s*false\s*$", yaml_text):
            problems.append(f"{name}: explicit-only skill lacks allow_implicit_invocation: false")
    upstream = entry.get("upstream")
    if (
        isinstance(upstream, dict)
        and upstream.get("license")
        and not any((source / candidate).is_file() for candidate in ("LICENSE", "LICENSE.txt"))
    ):
        problems.append(f"{name}: vendored skill is missing its upstream license file")
    return problems


def update_manifest(root: Path) -> None:
    manifest = load_manifest(root)
    for name, entry in _skills(manifest).items():
        entry["local_files"] = tree_hashes(root / CANONICAL / name)
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    (root / MANIFEST).write_text(text, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report drift; change nothing.")
    mode.add_argument("--update-manifest", action="store_true", help="Refresh local file hashes.")
    args = parser.parse_args(argv)
    try:
        if args.check:
            problems = check(args.root)
            for problem in problems:
                print(f"DRIFT {problem}")
            print("skills in sync" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        if args.update_manifest:
            update_manifest(args.root)
            print(f"updated {MANIFEST}")
            return 0
        actions = sync(args.root)
        print("\n".join(actions) if actions else "already in sync")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
