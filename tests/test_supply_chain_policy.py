"""Static guards for the supply-chain controls required by issue #16.

These read repository configuration only; CI exercises the scanners themselves.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
DEPENDABOT = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))


def test_dependabot_covers_every_dependency_ecosystem_we_ship() -> None:
    ecosystems = {entry["package-ecosystem"] for entry in DEPENDABOT["updates"]}

    assert {"pip", "npm", "docker", "github-actions"} <= ecosystems


def test_ci_scans_images_and_publishes_sboms_with_an_enforced_policy() -> None:
    assert CI.count("aquasecurity/trivy-action@") >= 4  # two SBOMs and two gating scans
    assert "format: cyclonedx" in CI
    assert "severity: HIGH,CRITICAL" in CI
    assert 'exit-code: "1"' in CI
    assert "ignore-unfixed: true" in CI
    assert "name: image-sboms" in CI


def test_the_image_scanner_action_is_pinned_to_a_commit_sha() -> None:
    uses = re.findall(r"uses: (aquasecurity/trivy-action@\S+)", CI)

    assert uses
    for reference in uses:
        assert re.fullmatch(r"aquasecurity/trivy-action@[0-9a-f]{40}", reference), reference


def test_ci_enforces_the_license_policy_and_the_policy_exists() -> None:
    assert "python -m scripts.check_licenses" in CI
    assert (ROOT / "security" / "license-policy.toml").is_file()
    assert (ROOT / "docs" / "security" / "license-policy.md").is_file()


def test_trivy_exceptions_are_reasoned_and_expire() -> None:
    lines = (ROOT / ".trivyignore").read_text(encoding="utf-8").splitlines()
    previous_comment = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            previous_comment = False
        elif stripped.startswith("#"):
            previous_comment = True
        else:
            assert previous_comment, f"exception without a reason comment: {line}"
            expiry = re.search(r"exp:(\d{4}-\d{2}-\d{2})", stripped)
            assert expiry, f"exception without an expiry: {line}"
            assert date.fromisoformat(expiry.group(1)) >= date.today(), f"expired: {line}"
            previous_comment = False


def test_threat_model_covers_the_required_areas() -> None:
    text = (ROOT / "docs" / "security" / "threat-model.md").read_text(encoding="utf-8").lower()

    for area in (
        "uploads and parser abuse",
        "prompt injection",
        "cross-tenant",
        "ssrf",
        "supply chain",
        "provider failure",
    ):
        assert area in text, area


def test_vector_encryption_waiver_is_time_bounded_and_linked() -> None:
    adr = (ROOT / "docs" / "adr" / "0021-vector-store-encryption-waiver.md").read_text(
        encoding="utf-8"
    )
    expiry = re.search(r"Expiry: (\d{4}-\d{2}-\d{2})", adr)

    assert expiry, "the waiver needs an explicit expiry date"
    # Deliberate forcing function: once the waiver expires, renew or replace the ADR.
    assert date.fromisoformat(expiry.group(1)) >= date.today(), "ADR 0021 waiver has expired"
    assert "0021-vector-store-encryption-waiver.md" in (
        ROOT / "docs" / "adr" / "README.md"
    ).read_text(encoding="utf-8")
