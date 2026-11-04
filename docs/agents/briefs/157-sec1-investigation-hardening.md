# Brief: #157 SEC-1 multi-agent investigation security hardening

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/157
Tier: strongest. Check mode: --full.

## Outcome
Every prompt-injection and role-abuse path through the multi-agent investigation has a named red-team
case that fails closed at a named control; every investigation route has tenant, capability, consent,
and rate-limit coverage that a new route cannot silently skip; the threat model describes all of it
and says what scripted cases cannot prove.

## Facts (verified against the code on 2026-11-02)
- `apps/api/rate_limit.py`: `enforce_rate_limit` is called per route. The only middleware
  (`apps/api/main.py:correlate_request`) does correlation and telemetry, so no investigation route
  was limited.
- `src/orchestration/tool_gateway.py`: every operation shares one pre-flight (`_operate`,
  `_operate_owner`). `SpecialistGateway` exposes five tool operations.
- `src/orchestration/review_prompts.py`: the Critic prompt holds no rows, numbers, or document text;
  the findings prompt holds recomputed rates and fenced citation-verified answer text.
- `packages/investigation/state.py:CHANNEL_OWNERS`: each state channel has exactly one writer.
- `docs/security/threat-model.md`: no investigation section.

## Do
1. Limit `create` (`investigation`) and plan and SQL decisions (`investigation_decision`) after the
   tenant check. Cancel and reads stay unlimited. New settings and env vars with documented defaults.
2. `tests/test_investigation_api_hardening.py`: route-enumerating tenant, capability, consent, and
   rate-limit tests, plus characterisations of the two PROD-1 findings that stay open.
3. `tests/test_investigation_red_team.py`: data-driven cases with IDs, through real gateways and
   graph with scripted adapters, each asserting the control that stopped it.
4. Fix PROD-1 findings 3 and 4 in their own commits. Characterise findings 1 and 2; do not fix them.
5. Update `docs/security/threat-model.md` and `docs/operations/rate-limits-and-embedding-cache.md`.

## Do not touch
Consent acceptance semantics, lifecycle ownership, `LIVE_ADMISSION_RELEASED`,
`REVIEWED_PROVIDER_EVIDENCE`, `FailClosedAdapters`, routing default, and prompt-bearing modules in
`evals/v1/prompt-registry.json`.

## Acceptance (from the issue)
- [ ] Every red-team case fails closed.
- [ ] Policy guards hold at every tool boundary, and the Critic cannot override controls.
- [ ] `python -m scripts.check_all --full` passes; no measured evaluation case changes; new telemetry
  or audit fields are allowlisted and content-free.

## Evaluation and docs impact
No prompt, retrieval, fixture, or safety behaviour in `evals/v1/` changes unless a case exposes a real
bypass. Threat model and rate-limit operations doc change. No ADR.

## Design decisions already made
- Cancel is never rate limited: a stop must not be refusable. Reads stay unlimited, as everywhere
  else in this API: the limiter is for expensive operations, and a 429 must not break status polling.
- Limits run after the tenant check, so a foreign ID is the same 404 and spends no counter.
- Scripted cases prove the deterministic controls hold whatever a model says. They do not measure how
  a real model behaves.
