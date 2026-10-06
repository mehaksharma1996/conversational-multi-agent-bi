# Brief: #9a Provider-neutral API authentication foundation

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full.

## Outcome

The FastAPI boundary can run either in an explicit local-development identity mode or in an OIDC
bearer-token mode. OIDC mode verifies signatures and required claims before deriving tenant scope;
malformed or partial identity configuration fails closed instead of silently falling back to local.

This slice does not complete issue #9. Browser sign-in/logout, roles and capabilities, and the final
cookie/token, CSRF, and CORS decision remain follow-up slices.

## Facts (verified against the code on 2026-10-05)

- `apps/api/dependencies.py:get_identity` always returns `LOCAL_DEV_TENANT_ID` and deliberately
  ignores browser-provided tenant headers.
- `packages/connectors/ports.py:IdentityContext` already carries `tenant_id`, `subject`, and
  `authentication_mode`; API routes authorize every resource through that context.
- `apps/api/main.py:create_app` is the composition root and already supports injected repositories,
  model clients, telemetry, and audit sinks.
- `config/settings.py:Settings` has no API identity configuration. `compose.yaml` remains
  loopback-only and must stay that way until all of issue #9 is complete.
- `Authlib==1.8.0` is a direct dependency and its installed `joserfc==1.7.5` package provides the
  non-deprecated JWT/JWK implementation. `httpx==0.28.1` is already locked.
- Cross-tenant negative tests already cover the resource graph by overriding `get_identity`; those
  tests must remain unchanged and passing.

## Do

1. Add fail-closed settings for `API_AUTH_MODE=local|oidc`, OIDC issuer, audience, JWKS URL,
   signing algorithms, clock skew, JWKS cache duration, and HTTP timeout.
2. Reject partial OIDC configuration, unsupported algorithms, non-HTTPS identity endpoints, and
   any OIDC settings supplied while local mode is selected.
3. Add an API identity provider that verifies bearer JWT signatures with cached JWKS, then validates
   `iss`, `aud`, `sub`, `exp`, and optional `nbf`/`iat` before deriving the tenant ID.
4. Inject the provider from `create_app`; keep local mode explicit and compatible with existing
   tests. Never accept a request tenant ID as authority.
5. Return correlation-aware, content-free 401 responses with `WWW-Authenticate: Bearer`; return a
   safe 503 when the configured JWKS service is unavailable.
6. Add deterministic offline unit/API tests for valid tokens, missing credentials, bad signatures,
   wrong issuer/audience, expired tokens, invalid configuration, and local compatibility.
7. Document every new setting in `.env.example`, Compose, and the authentication/operations docs.
   Regenerate OpenAPI and the TypeScript schema when the bearer security scheme changes.

## Do not touch

- Do not add browser redirects, callback routes, cookies, roles, or authorization capabilities in
  this slice.
- Do not expose Compose beyond loopback or remove the unauthenticated-local warning.
- Do not trust `X-Tenant-ID`, token tenant claims, email addresses, or client-selected issuers.
- Do not log or audit bearer tokens, claims, subjects, or identity-provider payloads.
- Do not weaken repository ownership checks or existing cross-tenant not-found behavior.

## Acceptance

- [ ] Verified OIDC claims derive tenant scope server-side; request data is never tenant authority.
- [ ] Production identity configuration fails closed when missing, partial, or malformed.
- [ ] Invalid or missing bearer tokens receive a safe 401 and provider outages receive a safe 503.
- [ ] Existing resource-level cross-tenant denial tests remain passing.
- [ ] Local development remains explicit, documented, and loopback-only.
- [ ] Python, OpenAPI, frontend, and deterministic evaluation checks pass.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety behavior changes, so `evals/v1/` and its baseline do
not change. Update ADR 0004 only through a separate superseding/addendum decision if the later
browser-transport slice changes its accepted boundaries.

## Design decisions already made

- Use signed bearer JWTs for this API foundation; browser session transport is deferred.
- Use `joserfc` directly rather than Authlib's deprecated `authlib.jose` facade.
- Allow only configured asymmetric signing algorithms; symmetric `HS*` and `none` are forbidden.
- Require explicit HTTPS issuer and JWKS URLs. Tests inject a JWKS fetcher and do not use a network.
- Cache JWKS in memory for a bounded interval. Durable/shared caches belong to later infrastructure
  work and do not weaken signature verification.
