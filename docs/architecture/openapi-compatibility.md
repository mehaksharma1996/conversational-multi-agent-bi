# OpenAPI compatibility gate

`python -m scripts.check_openapi_compatibility --base-ref <git-ref>` compares the committed
`openapi/openapi.json` with the artifact on the base ref. It rejects removed paths, operations,
parameters, response media types/statuses and object fields; newly required request bodies,
parameters, and fields; narrowed enums; and changed schema types. Local component references and
`allOf` object composition are resolved before comparison.

CI fetches full history, regenerates the artifact, checks it has no diff, and compares it with the
pull-request base SHA or the pre-push SHA. The TypeScript client remains independently reproducible
in the frontend job.

Intentional breaks require both an API version change and an ADR reference:

```powershell
$env:OPENAPI_BREAK_OVERRIDE = "ADR-0012"
python -m scripts.check_openapi_compatibility --base-ref origin/main
```

CI reads the same value from the `OPENAPI_BREAK_OVERRIDE` repository variable. It should be set only
for the reviewed breaking commit and removed immediately afterward. An arbitrary string, a missing
version bump, or a change without detected breaks cannot turn a failure into an unreviewed contract
change.
