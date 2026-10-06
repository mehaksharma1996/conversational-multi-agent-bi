# Brief: #9c Browser session core (cookie identity, CSRF, logout)

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full. Implements the transport decided in
[ADR 0013](../../adr/0013-browser-authentication-transport.md); builds on #9a and #9b.

## Outcome

The API can authenticate a browser by an opaque HttpOnly session cookie, enforce CSRF on state-changing
cookie requests, and end sessions. Sessions are created later by the #9d callback; this slice only
consumes and ends them. Provider tokens are never stored.

## Facts (verified on 2026-10-05)

- `apps/api/dependencies.py:get_identity` resolves Bearer identity through the injected provider; every
  `/api/v1` route depends on it and on a #9b capability.
- nginx proxies same-origin `/api/`; FastAPI installs no CORS middleware.
- `Settings.validate_identity_configuration` is the fail-closed gate called by `create_app`.

## Do

1. `apps/api/sessions.py`: bounded in-memory store keyed by SHA-256 of a 256-bit identifier; idle and
   absolute expiry; lifetime capped by verified identity expiry; revoke.
2. `get_identity`: Bearer first (no ambient credential, no CSRF); else cookie session in OIDC mode;
   else the provider decides (401 in OIDC, local identity in local mode). Session roles feed #9b.
3. CSRF on unsafe methods for cookie requests: `X-CSRF-Token` equals the session value (constant
   time) and `Origin` equals `WEB_ORIGIN`; unset `WEB_ORIGIN` fails closed. Error `403 csrf_failed`.
4. `GET /api/v1/auth/session` (csrf token, roles, expiry; `no-store`) and `POST /api/v1/auth/logout`.
5. Settings `WEB_ORIGIN`, `API_SESSION_MAX_AGE_SECONDS`, `API_SESSION_IDLE_TIMEOUT_SECONDS`, validated
   fail-closed; `WEB_ORIGIN` is rejected in local mode.
6. Regression test that the API emits no CORS headers.

## Do not touch

- No login/callback/token exchange (#9d), no React changes (#9e), no audit events (#9f).
- Do not weaken #9a/#9b safeguards. Do not log or return session identifiers, CSRF values, or subjects.
- Keep Compose loopback-only.

## Acceptance

- [ ] Cookie sessions scope tenant server-side; cross-tenant stays 404.
- [ ] Missing/wrong CSRF token or Origin on a cookie write is a safe 403; Bearer requests are unaffected.
- [ ] Unknown, expired, revoked, oversized cookies are a safe 401.
- [ ] Logout needs CSRF, revokes server-side, and expires the cookie (Secure, HttpOnly, SameSite=Lax).
- [ ] No CORS headers; local mode never reads a session cookie.
- [ ] `python -m scripts.check_all --full` passes; OpenAPI and TS schema regenerated.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety-behavior change; `evals/v1/` is unchanged.
