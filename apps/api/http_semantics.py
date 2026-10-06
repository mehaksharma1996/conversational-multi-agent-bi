"""Reusable OpenAPI prose for create-operation retry and retention contracts."""

WORKSPACE_CREATE_SEMANTICS = """
Creates a workspace. `Idempotency-Key` is optional and scoped to the authenticated tenant.
The same key replays the original workspace and returns `201`; because this operation has no client
representation, a same-key payload conflict cannot occur. The mapping is retained until the
workspace expires or is deleted. It survives a process restart when the deployment enables durable
metadata (the Compose topology does) and is otherwise lost on process restart. After a client
timeout, retry is safe only with the same key. Without a key, retry may create another workspace.
""".strip()

NON_IDEMPOTENT_CREATE_SEMANTICS = """
This synchronous create operation does not support `Idempotency-Key`. A duplicate request may create
another resource or repeat work and is not replayed; idempotency-key conflicts therefore do not
apply. The created resource follows its owning workspace's retention. It survives a process restart
when the deployment enables durable metadata (the Compose topology does) and is otherwise lost on
process restart. A client timeout or disconnect does not cancel server work, and the request must
not be retried automatically. Resolve the original outcome through its owning resource or ask the
user before submitting again. State/precondition conflicts return `409` and are not transient.
""".strip()

DOWNLOAD_SEMANTICS = """
Streams the complete immutable representation in bounded chunks. `Content-Length` and attachment
`Content-Disposition` are always present. Byte ranges are not supported (`Accept-Ranges: none`), so
a partial transfer must be restarted with a new `GET`. A timeout or disconnect stops only that
transfer; it does not delete the resource or cancel report generation. The `GET` is safe to retry.
""".strip()
