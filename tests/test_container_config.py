"""Static checks for the container release.

Docker is not required (or assumed) here: these tests read the Dockerfiles, compose file,
nginx config, and ignore rules and assert the security and reproducibility invariants the
release depends on. The Compose smoke test in CI exercises the real images.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
DOCKERFILE_API = (ROOT / "Dockerfile.api").read_text(encoding="utf-8")
DOCKERFILE_WEB = (ROOT / "Dockerfile.web").read_text(encoding="utf-8")
NGINX = (ROOT / "docker" / "nginx" / "default.conf").read_text(encoding="utf-8")
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.IGNORECASE)


def _instructions(dockerfile: str, name: str) -> list[str]:
    joined = dockerfile.replace("\\\n", " ")  # fold backslash line continuations
    return [
        " ".join(line.split())
        for line in joined.splitlines()
        if line.strip().upper().startswith(name.upper() + " ")
    ]


def _stages(dockerfile: str) -> list[str]:
    return _instructions(dockerfile, "FROM")


# --- compose hardening ---------------------------------------------------------------------


def test_expected_services_and_profiles() -> None:
    assert set(SERVICES) == {"api", "web", "streamlit"}
    assert SERVICES["streamlit"]["profiles"] == ["streamlit"]
    assert "profiles" not in SERVICES["api"] and "profiles" not in SERVICES["web"]


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_is_hardened(name: str) -> None:
    service = SERVICES[name]

    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["init"] is True
    assert service["restart"] == "unless-stopped"
    assert not service.get("privileged")
    assert service.get("network_mode") is None
    assert str(service.get("user", "10001")) not in {"0", "root"}
    assert "cap_add" not in service
    assert "/tmp" in service["tmpfs"][0]
    rendered = yaml.safe_dump(service)
    assert "docker.sock" not in rendered
    assert not re.search(r"^\s*-\s*[/.~][^:\n]*:", rendered, re.MULTILINE), "no host bind mounts"


def test_only_the_web_entry_points_are_published_and_only_on_loopback() -> None:
    assert "ports" not in SERVICES["api"]
    assert SERVICES["api"]["expose"] == ["8000"]
    for name in ("web", "streamlit"):
        for mapping in SERVICES[name]["ports"]:
            assert str(mapping).startswith("127.0.0.1:"), f"{name} must bind to loopback"


def test_secrets_are_interpolated_at_runtime_never_written_into_compose() -> None:
    for name, service in SERVICES.items():
        for variable, value in service.get("environment", {}).items():
            if SECRET_NAME.search(variable):
                assert str(value).startswith("${") and str(value).endswith(":-}"), (
                    f"{name}.{variable} must come from the environment, not a literal"
                )


def test_local_only_mode_and_model_key_are_runtime_switches_not_build_arguments() -> None:
    environment = SERVICES["api"]["environment"]

    assert environment["LOCAL_ONLY_MODE"] == "${LOCAL_ONLY_MODE:-false}"
    assert environment["GEMINI_API_KEY"] == "${GEMINI_API_KEY:-}"
    for service in SERVICES.values():
        assert "args" not in service.get("build", {}), "no build args that could carry secrets"


def test_deterministic_browser_providers_are_absent_from_production_configuration() -> None:
    production = "\n".join(
        [
            (ROOT / "compose.yaml").read_text(encoding="utf-8"),
            DOCKERFILE_API,
            DOCKERFILE_WEB,
        ]
    )

    assert "e2e_fake_app" not in production
    assert "deterministic-browser-test" not in production
    assert "compose.e2e.yaml" not in production


def test_api_state_lives_in_named_volumes_and_orphans_are_swept() -> None:
    api = SERVICES["api"]

    assert set(api["volumes"]) == {"bi-data:/data", "bi-audit:/audit", "bi-models:/models"}
    assert set(COMPOSE["volumes"]) == {"bi-data", "bi-audit", "bi-models"}
    assert api["environment"]["SWEEP_ORPHANED_WORKSPACES"] == "true"
    assert api["stop_grace_period"] == "30s"


def test_web_waits_for_a_healthy_api_and_images_use_local_tags() -> None:
    assert SERVICES["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    for service in SERVICES.values():
        assert service["image"].endswith(":local")
        assert "latest" not in service["image"]


def test_streamlit_profile_shares_the_api_image_and_has_its_own_healthcheck() -> None:
    streamlit = SERVICES["streamlit"]

    assert streamlit["image"] == SERVICES["api"]["image"]
    assert streamlit["healthcheck"]["test"][0] == "CMD"
    assert "_stcore/health" in " ".join(streamlit["healthcheck"]["test"])


def test_compose_environment_only_uses_variables_the_application_reads() -> None:
    settings_source = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    read_by_settings = set(re.findall(r'"([A-Z][A-Z0-9_]+)"', settings_source))
    third_party = {"HF_HUB_OFFLINE"}
    for name, service in SERVICES.items():
        for variable in service.get("environment", {}):
            assert variable in read_by_settings | third_party, f"{name}: unknown {variable}"


def test_every_interpolated_compose_variable_is_documented_in_env_example() -> None:
    documented = set(
        re.findall(
            r"^#?\s*([A-Z][A-Z0-9_]+)=",
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    interpolated = set(
        re.findall(r"\$\{([A-Z][A-Z0-9_]+)", (ROOT / "compose.yaml").read_text(encoding="utf-8"))
    )

    assert interpolated <= documented, sorted(interpolated - documented)


# --- Dockerfiles -----------------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", [DOCKERFILE_API, DOCKERFILE_WEB], ids=["api", "web"])
def test_dockerfiles_are_multi_stage_with_digest_pinned_bases(dockerfile: str) -> None:
    assert len(_stages(dockerfile)) >= 2
    base_arguments = [line for line in _instructions(dockerfile, "ARG") if "_IMAGE=" in line]
    assert base_arguments, "base images must be declared as pinned ARGs"
    for line in base_arguments:
        assert re.search(r"@sha256:[0-9a-f]{64}$", line), f"unpinned base image: {line}"
        assert ":latest" not in line
    for line in _stages(dockerfile):
        assert re.match(r"FROM \$\{[A-Z_]+_IMAGE\}( AS \w+)?$", line), line


@pytest.mark.parametrize("dockerfile", [DOCKERFILE_API, DOCKERFILE_WEB], ids=["api", "web"])
def test_dockerfiles_bake_no_secrets_and_declare_a_healthcheck(dockerfile: str) -> None:
    assert _instructions(dockerfile, "HEALTHCHECK")
    for instruction in ("ARG", "ENV"):
        for line in _instructions(dockerfile, instruction):
            names = re.findall(r"(?:^|\s)([A-Z_]+)=", line.split(" ", 1)[1] + " ")
            assert not any(SECRET_NAME.search(name) for name in names), line
    assert not re.search(r"COPY\s+[^\n]*\.env(\s|$)", dockerfile)
    assert "secrets.toml" not in dockerfile


def test_api_image_runs_as_a_fixed_non_root_user_with_a_read_only_friendly_layout() -> None:
    users = _instructions(DOCKERFILE_API, "USER")

    assert users == ["USER 10001:10001"]
    assert "--uid 10001" in DOCKERFILE_API
    for path in ("/data", "/audit", "/models"):
        assert path in DOCKERFILE_API
    assert "HOME=/tmp" in DOCKERFILE_API
    cmd = _instructions(DOCKERFILE_API, "CMD")[0]
    assert cmd.startswith('CMD ["uvicorn"'), "exec form so signals reach the server"
    assert "--timeout-graceful-shutdown" in cmd


def test_api_dependencies_install_reproducibly_from_the_lock_file() -> None:
    assert "-c requirements.lock" in DOCKERFILE_API
    assert "download.pytorch.org/whl/cpu" in DOCKERFILE_API
    assert "pip install --upgrade" not in DOCKERFILE_API
    assert "requirements-dev" not in DOCKERFILE_API


def test_web_image_uses_reproducible_installs_and_an_unprivileged_server() -> None:
    assert "npm ci" in DOCKERFILE_WEB and "npm install" not in DOCKERFILE_WEB
    assert "nginx-unprivileged" in DOCKERFILE_WEB
    for line in _instructions(DOCKERFILE_WEB, "USER"):
        assert line.split()[1].lower() not in {"root", "0"}, "never switch back to root"
    assert "EXPOSE 8080" in DOCKERFILE_WEB


def test_toolchain_versions_match_the_repository_declarations() -> None:
    node_version = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    python_tag = re.search(r"python:(\d+\.\d+)", DOCKERFILE_API)
    assert python_tag is not None
    minor = tuple(int(part) for part in python_tag.group(1).split("."))

    assert f"node:{node_version}-" in DOCKERFILE_WEB
    assert (3, 12) <= minor < (3, 15) and requires == ">=3.12,<3.15"


# --- ignore rules and attributes ------------------------------------------------------------------


def test_docker_ignore_keeps_secrets_state_and_dependencies_out_of_the_context() -> None:
    lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    for required in (".env", ".env.*", "data/", ".git/", ".venv/", "**/node_modules/", "backups/"):
        assert required in lines, required
    assert "!.env.example" in lines
    assert ".streamlit/secrets.toml" in lines


def test_line_endings_are_pinned_for_files_executed_in_linux_containers() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    for pattern in ("*.sh text eol=lf", "Dockerfile* text eol=lf", "compose.yaml text eol=lf"):
        assert pattern in attributes
    for script in (ROOT / "ops").glob("*.sh"):
        content = script.read_bytes()
        assert b"\r\n" not in content, f"{script.name} must use LF endings"
        assert b"set -eu" in content


# --- nginx --------------------------------------------------------------------------------------


def test_nginx_proxies_only_the_api_and_health_paths() -> None:
    locations = re.findall(r"location\s+(?:=\s+)?(\S+)\s*\{", NGINX)
    proxied = re.findall(r"location\s+(\S+)\s*\{[^}]*proxy_pass", NGINX, re.DOTALL)

    assert set(locations) == {"/nginx-health", "/api/", "/health/", "/"}
    assert set(proxied) == {"/api/", "/health/"}, "API docs and OpenAPI must not be proxied"
    assert "try_files $uri /index.html" in NGINX


def test_nginx_sends_security_headers_and_does_not_break_their_inheritance() -> None:
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
        "Permissions-Policy",
    ):
        assert f'add_header {header} "' in NGINX
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', NGINX)
    assert csp is not None
    for directive in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'"):
        assert directive in csp.group(1)
    assert "'unsafe-eval'" not in csp.group(1) and "script-src 'self';" in csp.group(1)
    assert "server_tokens off;" in NGINX
    # add_header inside a location would silently drop every inherited security header.
    _, locations = re.split(r"\n\s*location\s", NGINX, maxsplit=1)
    assert "add_header" not in locations


def test_nginx_body_limit_covers_the_largest_permitted_upload() -> None:
    limit = re.search(r"client_max_body_size\s+(\d+)m;", NGINX)
    assert limit is not None
    defaults = Settings(
        app_data_dir=Path("."),
        sqlite_db_path=Path("."),
        chroma_persist_dir=Path("."),
        gemini_api_key=None,
        gemini_model="model",
        embedding_model="embedding",
    )
    largest_upload_mb = max(defaults.max_tabular_upload_bytes, defaults.max_total_pdf_bytes) / (
        1024 * 1024
    )

    assert int(limit.group(1)) > largest_upload_mb


def test_nginx_resolves_the_api_at_request_time_so_upgrades_do_not_strand_the_proxy() -> None:
    assert "resolver 127.0.0.11" in NGINX
    assert "set $api_upstream http://api:8000;" in NGINX
    assert "proxy_pass http://api:8000" not in NGINX
