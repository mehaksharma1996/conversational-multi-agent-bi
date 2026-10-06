# Dependency license policy

Machine-readable policy: [`security/license-policy.toml`](../../security/license-policy.toml).
Enforcement: `python -m scripts.check_licenses` (CI `quality` job on ubuntu, Python 3.12). The check is
offline. It reads the installed distributions named in `requirements.lock` and the `license` fields in
`apps/web/package-lock.json`.

## Rules

- **Allowed:** permissive licenses (MIT, BSD, ISC, Apache-2.0, and similar), PSF-2.0, and
  file-level weak copyleft MPL-2.0 (consumed unmodified as libraries). `CC-BY-4.0` is allowed for
  build-time data only (caniuse-lite).
- **Expressions:** `A OR B` passes if either side is allowed; `A AND B` needs every part allowed.
- **Everything else fails**, including GPL, AGPL, LGPL, SSPL, proprietary terms, and unknown or missing
  license metadata. Strong copyleft is excluded because the repository is distributed as container
  images and a source tree whose combined-work obligations we do not want to take on.
- **Non-SPDX spellings** found in package metadata are mapped to the identifier they mean under
  `[aliases]`.

## Exceptions

An exception is an `[[exception]]` entry with `ecosystem`, `package`, a `reason`, and a `reviewed`
date. The checker refuses an entry without a reason or review date, and fails when an exception names
a package that is no longer in the lock files, so exceptions cannot silently outlive their cause.

To add one: confirm the license from the upstream repository or distribution (not from memory),
record what you checked, and say whether the package ships in the runtime image or only in tooling.

## Adding or upgrading a dependency

A Dependabot or manual bump that changes a license to something not allowed fails CI. Either replace the
dependency, or open a pull request that adds a reviewed exception and explains the obligation.

## Limits

The check trusts package metadata. It does not scan source files for embedded license headers, does not
inspect container base-image OS packages (see [image scanning](image-scanning.md) and the SBOMs), and
does not verify transitive dependencies that are absent from the lock files.
