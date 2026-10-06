from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def _locked_requirement(name: str) -> Requirement:
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        requirement = Requirement(candidate)
        if requirement.name.casefold() == name.casefold():
            return requirement
    raise AssertionError(f"{name} is not pinned in requirements.lock")


def test_pywin32_lock_is_windows_only() -> None:
    requirement = _locked_requirement("pywin32")
    assert requirement.marker is not None

    linux_environment = default_environment()
    linux_environment.update(sys_platform="linux", platform_system="Linux")
    windows_environment = default_environment()
    windows_environment.update(sys_platform="win32", platform_system="Windows")

    assert not requirement.marker.evaluate(linux_environment)
    assert requirement.marker.evaluate(windows_environment)
