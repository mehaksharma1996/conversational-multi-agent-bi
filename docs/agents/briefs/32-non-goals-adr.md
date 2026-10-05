# Brief: #32 Record deliberate non-goals in an ADR

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/32
Tier: small (documentation only). Check mode: fast (only the doc links matter).

## Outcome
`docs/adr/0012-deliberate-non-goals.md` exists, is `Accepted`, is indexed in `docs/adr/README.md`,
and the README "Current Limitations" section links to it. No code changes.

## Facts (verified 2026-10-05)
- ADRs 0001 to 0011 live in `docs/adr/`. `docs/adr/README.md` has an index table
  (`| [0011](0011-streamlit-disposition.md) | ... | Accepted |`) and a lifecycle section.
- Format to copy: `docs/adr/0011-streamlit-disposition.md` starts with `# ADR 0011: <title>`, then
  `- Status:`, `- Date:`, `- Issue:` lines, then `## Context and evidence` and decision sections.
  Read it once; do not read the other ADRs.
- Related decisions to link, not restate: ADR 0005 (job boundary, issue #10), ADR 0010 (container
  topology, restart-persistence waiver), ADR 0011 (Streamlit), the definition-of-done ledger
  `docs/architecture/definition-of-done.md` (hosting is a follow-up).
- Other open issues that change a small-surface decision: single provider is changed by #27, the
  SQLite-bound SQL guard by #28.

## Do
1. Write ADR 0012 with a non-goal table or sections. For every item give: what it is, why not now,
   and a concrete revisit trigger (evidence that would reopen it). Items, from the issue:
   Kubernetes/Helm/managed cloud; event-driven architecture and brokers; fine-tuning and model
   hosting; extra agent frameworks beyond LangGraph; knowledge graphs/GraphRAG; computer-use and
   browser agents; a full MLOps platform.
2. Add a short section recording the deliberately small surfaces already implied by the code:
   a single model provider today (link #27) and the SQLite-bound SQL guard (link #28).
3. Add the 0012 row to the `docs/adr/README.md` table.
4. In `README.md`, under "Current Limitations", add one bullet linking `docs/adr/0012-...md`.

## Do not touch
- Existing ADR text (the ADR README says accepted records are not rewritten).
- Any source, test, eval, or workflow file.

## Acceptance
- [ ] ADR 0012 is `Accepted`, same format as ADRs 0001 to 0011.
- [ ] Every non-goal has what, why not now, and a revisit trigger.
- [ ] README "Current Limitations" links to it; no contradiction with existing ADRs.
- [ ] Neutral wording: no claims about hiring or job markets.
- [ ] `python -m scripts.check_all --only ruff-check` passes (nothing else should change).

## Evaluation and docs impact
None for `evals/v1/`. Docs only.

## Design decisions already made
- Date the ADR with today's date. Status `Accepted`. Issue link `#32`, umbrella `#33`.
