# Brief: #9e React sign-in, sign-out, and expired-session handling

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full plus Playwright. Implements the browser half of
[ADR 0013](../../adr/0013-browser-authentication-transport.md); builds on #9a-#9d.

## Outcome

In OIDC mode the browser shows a sign-in screen until the API reports a session, sends the CSRF token
on state-changing requests, can sign out, and returns to sign-in when the session expires. Local
development mode is unchanged.

## Facts (verified on 2026-10-05)

- `apps/web/src/App.tsx` and its tests assume an authenticated API; its tests mock
  `./api/client`. `index.html` has a skip link to `#main-content`.
- `GET /api/v1/auth/session` is `401` when signed out, including in local mode, so it cannot by itself
  distinguish "local, no sign-in needed" from "OIDC, signed out".
- Mocked Playwright journeys use broad `**/api/v1/**` handlers that abort unknown paths.

## Do

1. API: public `GET /api/v1/auth/config` returning `{mode, login_available}`; no secrets.
2. Client: in-memory CSRF token (never storage/URL), `X-CSRF-Token` on non-safe methods through
   openapi-fetch middleware, a 401 handler for protected routes (not `/api/v1/auth/*`), `getAuthConfig`,
   `getBrowserSession` (null on 401), `endBrowserSession` (401 is success).
3. `AuthGate` wraps `App` without changing it: loading -> local | signed-in | signed-out | error.
   Sign-in is a full-page link to `/api/v1/auth/login`. The `auth_error` redirect parameter maps through
   a fixed allowlist to fixed messages and is removed from the URL.
4. Local mode renders no sign-in or sign-out controls.

## Do not touch

- No tokens in localStorage/sessionStorage, URLs, logs, or rendered text. No role-based UI gating
  (the server enforces authorization). No audit events (#9f).
- Compose stays loopback-only.

## Acceptance

- [ ] Signed-out users never see the application; unknown status never renders it.
- [ ] CSRF token attached to POST/PUT/DELETE only, cleared on sign-out and expiry.
- [ ] Expired or revoked sessions return to sign-in with a notice; the session probe's expected 401 does not.
- [ ] Unknown `auth_error` values (including `constructor`, `__proto__`, markup) are never reflected.
- [ ] Sign-in screen passes axe and the CSP check; skip link target exists.
- [ ] Vitest, Playwright, `scripts.check_all --full` pass; OpenAPI and TS schema regenerated.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety-behavior change; `evals/v1/` is unchanged. The OIDC
browser UX is tested against a mocked API; a live provider flow needs a real identity provider and
is not exercised here.
