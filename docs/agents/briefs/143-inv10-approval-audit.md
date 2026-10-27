# Brief: #143 INV-10 plan approval, SQL-wave approval, and investigation audit events

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/143
Tier: strongest. Check mode: --full.

## Outcome

An investigation runs only under a plan approval that a trusted approval service issued to an
identified approver, for the exact plan digest, scope, and authority epoch, inside a window that
ends at the earlier of 24 hours after approval and workspace expiry. SQL that requires approval is
prepared and guarded first, then paused as one batched approval request per dispatch wave, and
only the exact approved (or re-validated edited) statements execute. Follow-up rounds get a
production authorizer. Content-free `agent.investigation_*` and `agent.task_executed` audit events
record what actually happened.

## Facts (verified against `feee0d4` on 2026-10-27)

- `packages/investigation/lifecycle.py`: statuses and `TRANSITIONS`; `approval_deadline`,
  `approval_expired` (equality is expired). `AWAITING_PLAN_APPROVAL -> QUEUED | PLAN_REJECTED |
  EXPIRED`; `AWAITING_SQL_APPROVAL -> RUNNING | EXPIRED`; cancel, fail, interrupt from anywhere.
- `packages/investigation/state.py:PlanApprovalRecord` (digest, approved_at, deadline) is the only
  approval state; `FollowUpApproval` binds a round's `follow_up_digest`.
- `packages/investigation/gateway.py:InvestigationAuthority` carries the approval, epoch, and
  `sql_approval_required`; `check_binding`/`check_authority` are the pre-flight checks.
- `src/orchestration/investigation_execution.py`: `_verify_handoff` reads the proposal thread and
  requires equal digests and scope and an unexpired approval; `_claim` is a process-local replay
  registry. `FollowUpAuthorizer` is optional and absent in production.
- `src/orchestration/tool_gateway.py`: `prepare_sql` refuses `sql_approval_required` before a model
  call; `_PreparedSQL` already binds scope, digest, epoch, task, attempt, role, table identity, keyed
  fingerprint, policy status, `approval_required`, and a 10-minute expiry capped by the approval.
- `src/orchestration/investigation_checkpoint.py:guarded_node` lets LangGraph interrupts through.
- `packages/governance/audit.py:AUDIT_ACTIONS` and `AUDIT_ATTRIBUTES` are strict allowlists;
  `JsonlAuditSink` hash-chains per tenant. `agent.sql_approved`/`agent.sql_rejected` exist for v1.
- `apps/api/approvals.py` and `QuestionOrchestrator.resume_approval` are the v1 SQL approval path
  (LangGraph `interrupt` plus a typed resume). INV-10 reuses the same interrupt mechanism.

## Design decisions (approved in S0)

- **D1 One trusted approval service.** `src/orchestration/investigation_approval.py`
  (`InvestigationApprovalService`) is the only module that constructs `PlanApprovalRecord`,
  `FollowUpApproval`, plan-approval grants, and SQL-wave decisions. It holds the in-process
  lifecycle record of each investigation's decision stage. The proposal checkpoint is immutable
  evidence and is never rewritten. Typed, versioned, content-free contracts live in
  `packages/investigation/approval.py`. No second approval architecture is added.
- **D2 Plan approval.** A proposal is registered by the service (from the trusted
  `InvestigationProposalRun`, its planning epoch, a conversation ID, and the workspace expiry).
  It expires at `approval_deadline(registered_at, workspace_expires_at)`. Approval requires the
  registered proposal to still validate (`awaiting_plan_approval`, `proposed`, equal digests), the
  request to echo the exact scope, plan digest, and epoch, the approver to be a trusted
  `ApproverIdentity` of the same tenant with `analysis:run`, consent, and the live authority to be
  valid for the registered epoch. The grant's deadline is the earlier of `approved_at + 24h` and
  workspace expiry; equality is expired. Rejection moves to `plan_rejected`, expiry to `expired`;
  neither grants anything. Cancellation and revocation win over a concurrent approval (one lock).
- **D3 Mandatory grant.** `InvestigationExecutionRun` requires the service and an unconsumed grant
  whose record, scope, and epoch equal the authority's. The grant is consumed once. The existing
  handoff checks and `_claim` stay.
- **D4 One pending approval per conversation.** A conversation holds at most one live
  investigation in the approval service; a new proposal is refused while another is pending or
  approved and unfinished, and a SQL wave may be requested only by that investigation.
- **D5 SQL waves are the ready frontier.** With `sql_approval_required`, a SQL task is prepared
  (generated, single statement, read-only guard, schema compile, policy, keyed fingerprint, handle)
  and then *parked*. The pure dispatch policy keeps at most one *active* task; parked tasks hold
  approval-pending handles. When no active task remains and nothing else is ready, the policy asks
  for one approval of every parked statement: that set is the wave. A correction or retry needs a
  new approval and joins the next wave. Without approval required, behaviour is unchanged.
- **D6 Interrupt and resume.** A wave moves the run to `awaiting_sql_approval` and a guarded
  `sql_decision` step calls `interrupt` with a content-free wave reference only (no SQL). Resume
  never trusts the resume value: the owner reads the decision from the service and re-validates it
  against the wave (exact item cover, fingerprints, digest, scope, epoch, window). Outcomes:
  decided (resume `running`; approvals bound and rejections refused through the gateway),
  cancelled (`cancelled`), expired (`expired`, no report: `expired` is not a report status), or
  withdrawn (resume, and the next gateway call refuses by authority). A malformed decision is a
  defect (`failed`).
- **D7 Gateway binding.** Two owner-only operations: `bind_sql_approval` (re-verifies the decision
  item, the handle, the table identity, and authority; an edit is re-validated in full and gets a
  new fingerprint and handle) and `reject_sql` (ends the task `refused`, `sql_approval_rejected`,
  category `approval_rejected`). `execute_sql` runs an approval-required handle only with a bound,
  unexpired approval of its exact fingerprint. A handle stays single-use with a 10-minute TTL
  capped by the plan approval.
- **D8 Budget across interrupts.** The execution ledger is never reset. Its wall clock is the
  owner's pausable monotonic clock, paused only between the interrupt and the resume (bounded by
  the wave deadline), so a human's review time is not charged to the 240-second execution budget.
- **D9 Follow-ups.** The service implements `FollowUpAuthorizer` with an explicit, approver-bound
  pre-authorization of an exact `(scope, plan digest, epoch, round, follow_up_digest)`; without a
  matching pre-authorization the round is `not_authorized`. Agents never authorize.
- **D10 Audit.** Add `agent.investigation_{proposed,approved,rejected,cancelled,completed,
  reconciled}` and `agent.task_executed`, and allowlisted count/status attributes. The service
  emits them when the operation happens, with `tenant_id` and `resource_id` from the trusted scope.
  `completed` covers every terminal status other than `plan_rejected` and `cancelled`, with an
  `investigation_status` token. SQL-wave decisions reuse `agent.sql_approved`/`agent.sql_rejected`
  with counts. `agent.investigation_reconciled` is vocabulary only (INV-11 emits it).

## Do not touch

- The v1 question graph, router prompt, routes, and v1 SQL approval behaviour.
- INV-9 routing (default-off, proposal-only); `investigation_proposed` chat metadata never becomes
  approval authority.
- `evals/` cases, baselines, thresholds, and the prompt registry.
- `STATE_VERSION` (additive vocabulary only).

## Acceptance

- [ ] Rejection or expiry runs no SQL and no execution-model call; edited SQL is re-validated.
- [ ] Audit hash chains verify; events carry no question, plan, SQL, rows, excerpts, or prompts.
- [ ] The 25 adversarial and regression items in the INV-10 request have focused tests.
- [ ] `python -m scripts.check_all --full` passes; no measured evaluation case changes.

## Evaluation and docs impact

No evaluation or baseline change. ADR 0024 gains Amendment 8; the architecture doc and this brief
record the contracts and the INV-11 handoff.

## INV-11 handoff (out of scope here)

Tenant-scoped investigations API, background jobs, durable records, reports, and evidence;
approval and cancellation HTTP endpoints that build `ApproverIdentity` only from the verified
session; durable replay and idempotency (the service's registry is process-local); startup
reconciliation and `agent.investigation_reconciled`; showing a wave's SQL to the approver from the
service's in-process review, never from a checkpoint.
