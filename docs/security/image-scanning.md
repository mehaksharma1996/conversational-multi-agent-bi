# Image scanning, SBOMs, and base-image updates

## What CI does

The `containers` job in `.github/workflows/ci.yml` builds `conversational-bi/api:local` and
`conversational-bi/web:local`, then, before starting the stack:

1. Generates a **CycloneDX SBOM** for each image with Trivy and publishes both as the `image-sboms`
   build artifact.
2. **Scans each image** for vulnerabilities. The job fails on any **fixable HIGH or CRITICAL**
   finding (`--ignore-unfixed`). Findings with no available fix are visible in a local scan but do not
   gate a merge, because there is nothing to upgrade to; they are reviewed when the SBOM is.

The Trivy action is pinned to a commit SHA with the version in a comment, so Dependabot's
`github-actions` ecosystem proposes updates.

## Base-image digests

Both Dockerfiles pin base images by digest in literal `FROM` lines (not `ARG` defaults, which
Dependabot cannot update). Dependabot's `docker` ecosystem opens weekly pull requests for new
digests. Such a pull request must pass the `containers` job, including the image scan, the size
budget, and the smoke test, before merge. A digest bump that introduces a fixable HIGH or
CRITICAL finding fails the gate and is not merged until fixed or excepted.

## Exceptions

`.trivyignore` lists reviewed exceptions. Each entry needs a comment with the reason it does not apply
or is accepted, the compensating control, the reviewer and date, and an `exp:YYYY-MM-DD` expiry after
which Trivy stops honouring it. Prefer upgrading to excepting. An exception without a reason or expiry
should be rejected in review.

## Related policy

- Python dependency CVEs: `pip-audit` in the `security` job, with reasoned `--ignore-vuln` entries.
- Secrets: gitleaks in the `security` job.
- Licenses: [license policy](license-policy.md).
- Threat model: [threat-model.md](threat-model.md).

## Limits

Scanners report known vulnerabilities in packages they can identify. They do not find logic flaws,
malicious-but-unlisted packages, or vulnerabilities disclosed after the scan. Re-run on a schedule
by rebuilding when base images change.
