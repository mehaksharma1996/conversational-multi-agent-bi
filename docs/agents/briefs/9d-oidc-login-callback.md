# Brief: #9d OIDC login and callback (authorization code + PKCE)

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full. Implements the flow in
[ADR 0013](../../adr/0013-browser-authentication-transport.md); builds on #9a-#9c.

## Outcome

`GET /api/v1/auth/login` starts a PKCE sign-in and `GET /api/v1/auth/callback` completes it, creating
the #9c session. Provider tokens are exchanged server-side and discarded.

## Facts (verified on 2026-10-05)

- `OidcBearerIdentityProvider` already owns cached JWKS, the algorithm allowlist, issuer checks, and
  the roles claim; the ID token must reuse that, not a second verifier.
- `InMemorySessionStore.create(..., identity_expires_at=...)` already caps lifetime at token expiry.
- Callback URLs carry the single-use code and state in the query. uvicorn's access log and nginx's
  default format both print the full request line.

## Do

1. Settings (all-or-nothing, fail-closed): `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`,
   `OIDC_CLIENT_ID`, optional `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI` (= `WEB_ORIGIN` +
   `/api/v1/auth/callback`), `OIDC_SCOPES` (must contain `openid`). HTTPS endpoints only. Rejected in
   local mode. The secret is excluded from `repr`.
2. `verify_id_token`: signature, algorithm allowlist, `iss`, `aud == client_id`, `sub`, `exp`, and an
   essential `nonce` equal to the login's.
3. `apps/api/oidc_login.py`: single-use, 10-minute, bounded pending-login store keyed by `state`
   and bound to the browser by an HttpOnly `__Host-bi_login` cookie (login-CSRF defense); S256 challenge;
   token exchange with timeout, no redirects, 1 MB bound; 4xx = failed login, other = provider outage.
4. Routes redirect only to fixed paths (`/`, `/?auth_error=login_failed`,
   `/?auth_error=provider_unavailable`); provider `error` text and request parameters are never reflected.
5. Keep codes and state out of logs: `--no-access-log` for uvicorn; nginx `path_only` log format.
6. Not configured: both routes return 404 `login_not_configured`.

## Do not touch

- No React changes (#9e); no audit events (#9f); do not store or return access, refresh, or ID tokens.
- Do not weaken #9a-#9c safeguards. Compose stays loopback-only.

## Acceptance

- [ ] Valid flow creates a session whose lifetime never exceeds the ID token.
- [ ] Unknown, reused, expired, unbound state; bad nonce/audience/issuer/signature/expiry; missing
      `id_token`; provider 4xx/5xx/redirect/oversize; oversized code all fail safely with fixed redirects.
- [ ] Partial or unsafe login configuration fails startup.
- [ ] Tests fail when binding, nonce, or single-use checks are removed (mutation-checked).
- [ ] Container tests assert access logs omit query strings.
- [ ] `python -m scripts.check_all --full` passes; OpenAPI and TS schema regenerated.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety-behavior change; `evals/v1/` is unchanged.
Docker was unavailable, so the nginx log-format change is covered by static tests only.
