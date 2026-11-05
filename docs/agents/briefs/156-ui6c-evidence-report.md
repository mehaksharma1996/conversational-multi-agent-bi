# Brief: #156 UI-6c investigation evidence inspection and verified report

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/156
Tier: strongest. Check mode: --full.

## Outcome
A finished investigation shows its verified report in the Ask workspace, with an evidence inspector
(SQL and preview rows, document citations, derived measurements, the hypothesis board, critic
findings) and two-way navigation between claims and evidence. The report can be exported. No model
output is promoted, no evidence is created, and tampered content is never shown.

## Facts (verified against the code on 2026-11-02)
- `apps/api/investigation_store.py:load_report` checks only the **report** digest. The evidence
  column has no integrity binding, so "digest-checked evidence" needed a store change.
- Stored evidence is `{evidence_id: typed payload}`. `QueryEvidencePayload` deliberately has no SQL,
  and `EvidenceRecord` provenance is not stored, so **completed SQL did not survive the run**. The
  executed statement exists only in the gateway (`SQLEvidencePayload.sql`).
- `_settle` stores payloads for every terminal report, including `failed`, whose report must carry
  codes only (R8). A read must not reveal them.
- `GET /investigations/{id}` returns the report as an untyped dict (`workspace:read`). A tampered
  report reads as `report: null`.
- The verified report already holds `facts`, `hypotheses`, `critiques`, `query_tables`, `citations`.
- `tool_gateway.py` and `investigation_execution.py` are not prompt-registry targets.
- The Ask workspace uses hash routing, so in-page `href="#..."` anchors would change section.

## Do
1. Store: bind report and evidence in one digest (no metadata migration). Keep a report-only match
   as a legacy record: the report stays readable, its evidence reads as unavailable.
2. Execution: expose executed SQL on the terminal result, read from the gateway's verified evidence.
3. Runtime: store an evidence envelope; add a read-only `verified_content(record)` accessor.
4. API: `GET /investigations/{id}/evidence` and `GET /investigations/{id}/report/export`.
5. Regenerate OpenAPI and web types; add client functions.
6. Web: `reportModel.ts`, `useInvestigationReport.ts`, report view, evidence inspector.
7. Tests: pytest, Vitest, mocked Playwright, scripted real-stack journey with a v2 plan.

## Do not touch
Verifier, report composition, payload contracts, prompts, evaluation baseline, adapters, routing
default, approval, budget, consent, `LIVE_ADMISSION_RELEASED`, `REVIEWED_PROVIDER_EVIDENCE`.

## Acceptance
- [ ] Evidence inspector covers SQL and preview rows, document citations, hypothesis board, critic
  findings.
- [ ] Report view with export; every finding links to its evidence.
- [ ] Accessibility and reliability are verified.
- [ ] `python -m scripts.check_all --full` passes; no measured evaluation case changes.

## Evaluation and docs impact
No prompt, retrieval, fixture, or safety behaviour changes, so `evals/v1/` is unchanged. Update the
route table in `docs/architecture/investigation-workflow.md` and add ADR 0024 Amendment 12, because
executed SQL is now retained in the encrypted workspace store.

## Design decisions already made
- **E1. One read contract.** `GET /api/v1/investigations/{id}/evidence` returns the typed
  `VerifiedReport` and typed evidence from one digest-checked read, so report and evidence can never
  disagree. `InvestigationResponse.report` stays as it is, for compatibility.
- **E2. Capability: `workspace:read`, with SQL text gated on `analysis:run`.** Whoever can read the
  report can check it. Preview rows are capped at the report's own limit (50), and citations are
  locations only, so a viewer gains nothing beyond what the report already shows. SQL text reveals
  schema and filter literals and is the material `analysis:run` already gates in the wave review, so
  a viewer gets `sql: null`, `sql_state: "restricted"`. Widening later is compatible; narrowing is
  not. This is decided per request from the verified identity.
- **E3. One digest binds both.** `report_sha256` now digests the report and evidence together.
  Tampering with either refuses both (409 `investigation_content_unverifiable`). This needs no
  metadata migration, so it cannot collide with parallel work. The column name is slightly narrower
  than its meaning; the store docstring states it.
- **E4. Availability.** Unknown, foreign, non-terminal, or report-less: 404, as other reads. A report
  is withheld until the record is terminal even though it is stored just before. `failed` reports
  return `evidence_state: "not_reported"` with no evidence (R8). Legacy records return
  `evidence_state: "unavailable"`.
- **E5. No answer prose.** The document's model-written answer is not returned. The report excludes
  it by design, and the passage text was never stored. Citations show filename, page, chunk, score,
  and verification status only. Adding the quoted answer later is a compatible change.
- **E6. Export is server-side, under `report:export`.** `GET .../report/export?format=markdown|json`
  renders the digest-checked report as an attachment with `Cache-Control: private, no-store` and
  audits `report.downloaded`. Markdown escapes every untrusted slot and defuses links, HTML, images,
  and spreadsheet formula prefixes. Exports contain no SQL. There is no CSV format.
- **E7. Claim states are computed, not stored.** `verified` (every reference resolves to returned
  evidence), `unsupported` (a reference does not resolve, or a measurement or decisive verdict cites
  nothing), `incomplete` (untestable, unevaluated, or a stated limitation). An unresolved reference
  renders as plain text, never a link.
- **E8. Navigation** uses buttons plus managed focus, not hash anchors. A click is user intent, so it
  may move focus; loading and polling never do, and terminal focus skips when the person is typing.
- **E9. Evidence is read once per investigation**, on reaching a reportable terminal status, keeps
  SQL in component memory only, and is dropped on scope change. Errors offer an explicit retry.

- **E10. "Verified" states its limit (final review, 2026-11-04).** The verifier recomputes numbers
  from the rows a query returned; it cannot tell whether those rows reflect the source data. A
  read-only statement with invented counts passes every deterministic control, and once approved a
  refuted hypothesis reads as supported (SEC-1's RT-ROW-05 residual). This is an engine boundary, so
  UI-6c does not change it. It only stops overstating it: the report view and both exports always
  state what verification covers, and the claim chip reads "Traced to evidence". Until the engine
  gains a control, no release may claim that a verified report proves its numbers are correct.
  (Superseded: ADR 0024 Amendment 13 added that control. Its own limits are stated there.)
- **E11. Claim states fail towards doubt (final review).** A claim naming a fact or hypothesis the
  report does not hold is `unsupported` whatever else it cites. Recorded conflict is `conflicted`, an
  open follow-up is `incomplete`, and a report with no evidence behind it does not show its
  "verified" label; its export states the evidence state.

## Delivery record
The scripted stack does reach a full report: `build_scripted_report_app` runs the INV-7 churn
plan on the real sample data with scripted model answers, through the real gateway, verifier, and
store. Its journeys prove browser and API wiring only. They say nothing about a production model.
Docker journeys were not run: Docker is unavailable on this host.
