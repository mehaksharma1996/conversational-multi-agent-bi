# Brief: #9b Server-owned API authorization

Issue: https://github.com/mehaksharma1996/conversational-multi-agent-bi/issues/9
Tier: strongest. Check mode: --full. Builds on #9a (PR #43).

## Outcome

Every `/api/v1` operation requires a server-owned capability. Capabilities come only from roles in a
verified OIDC token (mapped by trusted server code) or from explicit local-development mode. A
missing capability yields a safe, correlation-aware 403. Tenant scope is still derived server-side
from the verified subject; request data is never authority for tenant, role, or capability.

This slice does not complete issue #9: browser sign-in, cookies/CSRF/CORS, logout, and auth audit
events remain follow-up slices.

## Facts (verified against the code on 2026-10-05)

- `packages/connectors/ports.py:IdentityContext` carries `tenant_id`, `subject`,
  `authentication_mode`; no roles yet.
- `apps/api/auth.py:OidcBearerIdentityProvider.authenticate` validates the JWT, then builds the context.
- Routes live in `apps/api/routes.py` and `apps/api/feature_routes.py`; each uses
  `Depends(get_identity)`. Existing tests override `get_identity` with plain
  `IdentityContext(tenant_id=...)` (local mode); those must keep passing unchanged.
- `apps/api/errors.py:ApiError` already supports status, code, and headers.

## Design

Capabilities (server vocabulary, `apps/api/authorization.py`):

| Capability        | Operations |
|-------------------|------------|
| `workspace:read`  | every GET of workspace/dataset/analysis/collection/conversation/message metadata, sheets listing |
| `data:write`      | create workspace, tabular upload, create dataset, confirm schema mapping, create document collection |
| `analysis:run`    | create analysis, create conversation, post message, SQL approval decision |
| `report:export`   | create/read reports and exports |
| `workspace:admin` | accept Gemini consent, delete workspace |

Roles (server-owned mapping; unknown roles grant nothing):

- `viewer`: `workspace:read`
- `analyst`: viewer + `data:write`, `analysis:run`, `report:export`
- `workspace_admin`: analyst + `workspace:admin`

Role source: the verified token claim named by `OIDC_ROLES_CLAIM` (default `roles`), only when it is a
list of at most 32 strings of at most 64 characters each; any other shape yields no roles. Claims
named `tenant_id`, `capabilities`, or similar are ignored.

Local mode (`authentication_mode == "local"`) explicitly holds every capability. It is loopback-only
single-user development and is documented as such. No other mode ever receives implicit capabilities.

Enforcement: `require_capability(Capability.X)` is a FastAPI dependency (resolves `get_identity`
first, so anonymous = 401 before 403) attached to every route via `dependencies=[...]`. A denial
raises `AuthorizationError` (403, code `permission_denied`, generic message, correlation ID, no
capability or role names echoed).

## Do not touch

- Do not add browser flows, cookies, CSRF, CORS changes, or audit events in this slice.
- Do not read roles or capabilities from headers, query, body, or path.
- Do not weaken #9a token/JWKS safeguards or repository ownership checks (cross-tenant stays 404).

## Acceptance

- [ ] Every protected route declares a capability; a test enumerates routes and fails on any gap.
- [ ] Anonymous, missing-capability, unknown-role, malformed-roles, and request-supplied
      tenant/role/capability attempts are denied or ignored in negative tests.
- [ ] Cross-tenant access still returns not-found.
- [ ] Local-development permissions are documented.
- [ ] `python -m scripts.check_all --full` passes; OpenAPI and generated TS schema updated.

## Evaluation and docs impact

No prompt, retrieval, model, fixture, or safety-behavior change; `evals/v1/` is unchanged.
