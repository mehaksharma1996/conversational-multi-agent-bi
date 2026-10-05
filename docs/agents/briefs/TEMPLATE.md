# Brief: #<issue> <title>

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/<issue>
Tier: <small | medium | strongest>. Check mode: <fast | --full>.

## Outcome
Two or three sentences: what is true after this change that is not true now.

## Facts (verified against the code on <date>)
- `path/to/file.py:<symbol>`: what it does today, in one line each. Only what the agent needs.

## Do
Numbered, concrete steps in the order to do them. Name functions, files, and tests.

## Do not touch
Files, behaviors, or decisions that must stay as they are, and why (safety or boundary rules).

## Acceptance (copy from the issue, trimmed to checkable items)
- [ ] ...

## Evaluation and docs impact
Whether `evals/v1/` and its baseline change, which doc to update, and whether an ADR is needed.

## Design decisions already made
Choices the agent must not reopen (library, naming, limits). Prevents long deliberation.
