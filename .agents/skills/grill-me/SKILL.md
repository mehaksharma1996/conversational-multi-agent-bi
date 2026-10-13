---
name: grill-me
description: Explicit use only. Interview the user, 1-3 questions per round, to sharpen a plan or design.
argument-hint: "The plan, decision, or idea to stress-test"
disable-model-invocation: true
---

Adapted from `mattpocock/skills` `grill-me` plus its `grilling` dependency, both at 49dd158d (MIT,
see `LICENSE`). The `grilling` method is inlined here, with no separate `grilling` skill and no
cross-skill call, so nothing can start from an ordinary prompt. Local changes: at most 3 questions
per round, no sub-agents, read-only.

Run only because the user invoked this skill. If it is invoked during an implementation, grill the
named decision only, then stop and hand control back.

## Method

Map the plan as a **design tree**: every decision branches into the decisions that depend on it.
The **frontier** is every decision whose prerequisites are settled. Each round, pick the **1 to 3
frontier questions** that matter most (the ones that unblock the most other decisions or carry the
most risk) and ask them; never more than three.

Format each question, with your recommended answer, worded so "yes" accepts it:

```
❓ **Q1** - **<title>**: <question; include options when useful>

➡️ <your recommended answer>
```

Then wait. Each answer reshapes the tree: recompute the frontier and ask the next round. A
question whose answer depends on one still open belongs to a later round. Challenge ambiguous
terms, unjustified assumptions, missing constraints, and unmeasurable acceptance criteria.

## Facts versus decisions

Finding facts is your job: look them up yourself with read-only tools (read files, search, `git
log`). Do not spawn sub-agents. Do not ask the user for anything you could look up. Decisions are
the user's: ask, and wait. Never answer your own questions.

## Limits

This skill is read-only: no file writes, no commits, no GitHub or network actions. If the user
wants the outcome recorded, offer to copy `docs/agents/templates/decision-log.md` to
`work/decisions/<slug>.md` (git-ignored) and write it only after they say yes.

## Finish

Done when the frontier is empty and nothing is silently assumed. Summarize resolved decisions,
unresolved decisions, and measurable acceptance criteria. Do not act on the outcome until the user
confirms the shared understanding.
