# ADR 0013: Browser authentication transport

- Status: Accepted
- Date: 2026-10-05
- Issue: [#9](https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9)
- Finalizes the transport, CSRF, and CORS details that [ADR 0004](0004-identity-session-tenancy.md)
  deferred. It does not supersede ADR 0004.

## Context

#9a added OIDC bearer-JWT verification and #9b added server-owned capabilities, both for direct API
clients. The React client is served by nginx on one origin that reverse-proxies `/api/` and
`/health/` to FastAPI (`docker/nginx/default.conf`); FastAPI installs no CORS middleware. The client
holds no credentials, and API state is process-local (ADR 0010), so a restart already clears all
workspace data.

A browser needs a sign-in flow, a way to carry the authenticated identity on later requests, protection
for state-changing requests, and a defined sign-out/expiry behavior.

## Options considered

| Option | Token exposure | Cost |
|---|---|---|
| Browser-held access token (SPA does code+PKCE, keeps token in memory) | Token readable by any script on the page; lost on reload; refresh needs a token in storage or a hidden iframe | Smaller backend; larger XSS blast radius |
| Backend-managed session (BFF): API runs code+PKCE, discards provider tokens, issues an opaque HttpOnly cookie | Provider tokens never reach the browser; the cookie is unreadable by script | Needs a session store and CSRF defense |

## Decision

Use a **backend-for-frontend session**. It has the smallest token exposure surface, fits the existing
same-origin proxy, and keeps the browser free of OAuth logic.

1. **Flow.** Authorization Code with PKCE (S256). `GET /api/v1/auth/login` creates a single-use,
   10-minute pending login holding `state`, `nonce`, and the PKCE verifier, bound to the browser by a
   short-lived `HttpOnly` cookie, then redirects to the provider. `GET /api/v1/auth/callback` rejects
   unknown, reused, expired, or unbound `state`; exchanges the code server-side; verifies the ID token
   (signature, algorithm allowlist, `iss`, `aud` = client ID, `exp`, `nonce`) with the #9a verifier;
   creates the session; and redirects to a fixed path. The post-login redirect target is never
   client-supplied, so there is no open redirect.
2. **No provider tokens retained.** After the callback the access, ID, and refresh tokens are discarded.
   The session stores only the subject, derived tenant, verified roles, and expiry. There is no refresh
   token; when the session ends, sign-in repeats (the provider may complete it silently).
3. **Session cookie.** 256-bit random opaque identifier, `Secure`, `HttpOnly`, `SameSite=Lax`,
   `Path=/`, no `Domain`, named with the `__Host-` prefix. Only a hash of the identifier is kept
   server-side. Lifetime is the earliest of an absolute limit (default 8 hours), an idle timeout
   (default 30 minutes), and the ID-token expiry.
4. **Identity resolution.** `get_identity` accepts either a verified Bearer token (API clients, no
   ambient credential) or a valid session cookie. Both produce the same `IdentityContext`, so #9b
   capabilities apply identically. Tenant, role, and capability are never read from request input.
5. **CSRF.** Unsafe methods (`POST`, `PUT`, `PATCH`, `DELETE`) authenticated by cookie must carry
   `X-CSRF-Token` equal to the session's per-session CSRF value (compared in constant time), and any
   `Origin` header must match the configured web origin. `GET /api/v1/auth/session` returns the CSRF
   value; the SPA keeps it in memory only. Bearer-authenticated requests are exempt because the
   credential is not sent automatically by the browser.
6. **CORS.** The product is same-origin through nginx, so the API sends **no** CORS headers and no
   cross-origin caller is allowed. A cross-origin deployment requires a new decision.
7. **Logout and expiry.** `POST /api/v1/auth/logout` (CSRF-protected) deletes the server-side session
   and expires the cookie. An expired or unknown session yields the same safe 401 as a missing credential.
   Restarting the API ends all sessions (consistent with ADR 0010).
8. **Secrets hygiene.** Tokens, authorization codes, `state`, `nonce`, verifiers, session identifiers, and
   CSRF values never appear in logs, telemetry, audit payloads, error bodies, `localStorage`, or any URL
   after the callback has been processed.

## Delivery slices

- #9c: session store, cookie identity, CSRF, Origin check, `auth/session`, `auth/logout`, settings,
  no-CORS regression test.
- #9d: `auth/login` and `auth/callback` (PKCE, state, nonce, token exchange, ID-token verification).
- #9e: React sign-in, sign-out, CSRF header, and expired-session handling; end-to-end browser tests.
- #9f: allowlisted authentication/authorization audit events.

Compose stays loopback-only until every slice above passes.

## Consequences

- A script-injection bug cannot read provider tokens or the session identifier, but can still act as the
  user while the page is open; the existing CSP (`script-src 'self'`) remains the primary defense.
- The in-memory session store limits the API to one process, matching the current repository.
  A shared store requires the persistence work in ADR 0006.
- Session-bound CSRF plus `SameSite=Lax` plus an Origin check gives defense in depth.

## Invariants

- A request-supplied tenant, role, capability, session identifier shape, or redirect target is never
  authority.
- Production identity configuration remains fail-closed; a missing or partial session/provider setting
  never activates local mode.
