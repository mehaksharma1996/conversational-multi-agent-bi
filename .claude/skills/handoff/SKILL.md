---
name: handoff
description: Write a compact handoff for another agent session (Codex or Claude Code). Explicit use only.
argument-hint: "What will the next session be used for?"
disable-model-invocation: true
---

Adapted from `mattpocock/skills` `handoff` at 49dd158d (MIT, see `LICENSE`). Local changes: the
file goes to the repo's git-ignored `work/handoffs/` (not the OS temp directory), Git state is
recorded by a script, and the content is validated.

1. Run `python -m scripts.agent_handoff new --slug <short-task-label>`. It prints the new file's
   path, pre-filled with the live branch, commit SHA, and changed file names.
2. Replace every `_TODO_` with compact content (whole file at most 8 KB). If the user passed
   arguments, tailor the doc to what the next session will do.
   - Do not duplicate specs, plans, ADRs, issues, commits, or diffs: reference them by path, URL, or SHA.
   - Separate **Test evidence** (command and result you actually saw this session) from
     **Unverified claims**.
   - **Approval boundaries**: list only approvals the user actually gave. State that push, PR,
     merge, deploy, and any GitHub write need fresh approval unless the user says otherwise.
   - **Suggested skills**: name the skills the next session should invoke (normally `resume` first).
3. Do not open unrelated or untracked files just to describe them; name them and say they were
   not reviewed.
4. Never include secrets, tokens, `.env` contents, uploaded or private data, personal information,
   absolute user paths, or conversation transcripts.
5. Run `python -m scripts.agent_handoff check <path>` and fix every reported problem.
6. Tell the user the path. Do not commit it: `work/` is git-ignored.
