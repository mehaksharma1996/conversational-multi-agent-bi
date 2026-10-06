"""Check dependency licenses against the repository policy.

Python dependencies come from the installed distributions named in ``requirements.lock``;
npm dependencies come from the ``license`` fields in ``apps/web/package-lock.json``. Every
license must be allowed by ``security/license-policy.toml`` or the package must have a reviewed,
dated exception there. The check is offline and read-only.

    python -m scripts.check_licenses [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "security" / "license-policy.toml"
LOCK_PATH = ROOT / "requirements.lock"
NPM_LOCK_PATH = ROOT / "apps" / "web" / "package-lock.json"

_LICENSE_CLASSIFIER = "License :: "
_SPLIT = re.compile(r"\s+(?:AND|OR|and|or)\s+|[();/]")
_CLASSIFIER_IDS = {
    "OSI Approved :: MIT License": "MIT",
    "OSI Approved :: BSD License": "BSD",
    "OSI Approved :: Apache Software License": "Apache-2.0",
    "OSI Approved :: ISC License (ISCL)": "ISC",
    "OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
}


_TEXT_TITLES = {
    "mit license": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    "apache license": "Apache-2.0",
}


@dataclass(frozen=True)
class Policy:
    allowed: frozenset[str]
    aliases: Mapping[str, str]  # non-SPDX spelling -> SPDX identifier
    exceptions: Mapping[tuple[str, str], str]  # (ecosystem, package) -> reason

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> Policy:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        exceptions: dict[tuple[str, str], str] = {}
        for entry in raw.get("exception", []):
            reason = str(entry.get("reason", "")).strip()
            if not reason or not entry.get("reviewed"):
                raise ValueError(f"exception for {entry.get('package')} needs reason and reviewed")
            exceptions[(entry["ecosystem"], _normalise_name(entry["package"]))] = reason
        return cls(frozenset(raw["allowed"]["spdx"]), dict(raw.get("aliases", {})), exceptions)

    def tokens(self, expression: str) -> set[str]:
        return {self.aliases.get(token, token) for token in _tokens(expression)}


@dataclass(frozen=True)
class Violation:
    ecosystem: str
    package: str
    license: str


def _normalise_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _tokens(expression: str) -> set[str]:
    return {token.strip() for token in _SPLIT.split(expression) if token.strip()}


def python_license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith(_LICENSE_CLASSIFIER)]
    ids = [
        _CLASSIFIER_IDS.get(
            c.removeprefix(_LICENSE_CLASSIFIER), c.removeprefix(_LICENSE_CLASSIFIER)
        )
        for c in classifiers
    ]
    if ids:
        return " OR ".join(ids)
    legacy = (meta.get("License") or "").strip()
    if legacy and len(legacy) <= 60 and "\n" not in legacy:
        return legacy
    # A long legacy field is the license text itself; recognise it by its title line only.
    title = legacy.splitlines()[0].strip().lower() if legacy else ""
    return _TEXT_TITLES.get(title, "UNKNOWN")


def locked_python_packages(lock: Path = LOCK_PATH) -> list[str]:
    names = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==", line)
        if match:
            names.append(match.group(1))
    return names


def python_licenses(names: Iterable[str]) -> dict[str, str]:
    """Licenses of the locked packages installed here; platform-specific ones may be absent."""
    found: dict[str, str] = {}
    for name in names:
        try:
            found[name] = python_license(metadata.distribution(name))
        except metadata.PackageNotFoundError:
            continue
    return found


def npm_licenses(lock: Path = NPM_LOCK_PATH) -> dict[str, str]:
    packages: dict[str, Any] = json.loads(lock.read_text(encoding="utf-8"))["packages"]
    found: dict[str, str] = {}
    for path, info in packages.items():
        if not path:  # the root project
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        license_value = info.get("license")
        if isinstance(license_value, dict):  # legacy {"type": "MIT"}
            license_value = license_value.get("type")
        found[name] = str(license_value) if license_value else "UNKNOWN"
    return found


def evaluate(
    ecosystem: str, licenses: Mapping[str, str], policy: Policy
) -> tuple[list[Violation], list[str]]:
    """Return (violations, exceptions used)."""
    violations: list[Violation] = []
    used: list[str] = []
    for package, license_text in sorted(licenses.items()):
        key = (ecosystem, _normalise_name(package))
        # "A OR B" is satisfied by any allowed alternative; "A AND B" needs every part allowed.
        parts = re.split(r"\s+AND\s+", license_text, flags=re.IGNORECASE)
        ok = all(policy.tokens(part) & policy.allowed for part in parts)
        if ok:
            continue
        if key in policy.exceptions:
            used.append(f"{ecosystem}:{package}")
            continue
        violations.append(Violation(ecosystem, package, license_text))
    return violations, used


def stale_exceptions(policy: Policy, seen: Mapping[str, Iterable[str]]) -> list[str]:
    present = {
        (ecosystem, _normalise_name(name)) for ecosystem, names in seen.items() for name in names
    }
    return sorted(f"{eco}:{pkg}" for (eco, pkg) in policy.exceptions if (eco, pkg) not in present)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)

    policy = Policy.load()
    python = python_licenses(locked_python_packages())
    npm = npm_licenses()
    violations: list[Violation] = []
    exceptions_used: list[str] = []
    for ecosystem, found in (("python", python), ("npm", npm)):
        found_violations, used = evaluate(ecosystem, found, policy)
        violations += found_violations
        exceptions_used += used
    stale = stale_exceptions(policy, {"python": python, "npm": npm})

    if args.json:
        print(
            json.dumps(
                {
                    "violations": [v.__dict__ for v in violations],
                    "exceptions_used": exceptions_used,
                    "stale_exceptions": stale,
                    "python_packages": len(python),
                    "npm_packages": len(npm),
                },
                indent=2,
            )
        )
    else:
        print(f"checked {len(python)} Python and {len(npm)} npm packages")
        for violation in violations:
            print(f"VIOLATION {violation.ecosystem}:{violation.package}: {violation.license}")
        for name in stale:
            print(f"STALE exception (package no longer present): {name}")
    return 1 if violations or stale else 0


if __name__ == "__main__":
    sys.exit(main())
