# Brief: #9f Authentication and authorization audit events

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full. Builds on #9a-#9e; completes the audit item in ADR 0013.

## Outcome

Sign-in, sign-out, CSRF rejections, and authorization denials are recorded as append-only,
hash-chained, allowlisted audit events without any sensitive value, and without letting anonymous
callers grow the log.

## Facts (verified on 2026-10-05)

- `packages/governance/audit.py` has a closed action list, a typed attribute allowlist, and requires
  a path-safe tenant ID. `reason`, `outcome`, and `authentication_mode` already exist as tokens.
- Tenant IDs derived from a subject are 32 hex characters; `anonymous` cannot collide.
- Failure of the audit sink is swallowed by `AuditRecorder` (the action already happened).

## Do

1. Actions: `auth.login_succeeded`, `auth.login_failed`, `auth.logout`, `auth.csrf_rejected`,
   `authz.denied`. New attribute `capability` (token). Reserved tenant `ANONYMOUS_TENANT_ID`.
2. `ApiObservability.audit_tenant` for tenants from verified server state.
3. Emit: callback success (tenant from verified subject), logout (session tenant), CSRF rejection
   (session tenant), capability denial (identity tenant, capability name).
4. `LoginFailedError` carries a reason token and an `audited` flag set only after the pending login
   matched its `state` and binding cookie; only then is `auth.login_failed` written (`anonymous`).
5. Do not audit missing/invalid credentials, unknown `state`, or unbound callbacks (telemetry only).
6. Update `docs/governance/audit-and-observability.md` including the residual flood risk.

## Do not touch

- No subject, token, code, state, nonce, CSRF value, session ID, role list, or provider text in events.
- No new audit sink, rotation, or rate limiter; no change to `evals/v1/`.

## Acceptance

- [ ] Each event is produced exactly once for its trigger, under the right tenant, with only
      allowlisted attributes and no `dropped_attributes`.
- [ ] Anonymous garbage produces zero audit events (mutation-checked).
- [ ] Request-supplied tenant never selects the audit tenant.
- [ ] A failing sink does not fail sign-in or the 403.
- [ ] The default JSONL sink chain verifies for user and `anonymous` tenants.
- [ ] `python -m scripts.check_all --full` passes.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety-behavior change. Audit attributes remain an allowlist.
Residual risk: a caller who starts real logins and fails them can append to the `anonymous` file.
