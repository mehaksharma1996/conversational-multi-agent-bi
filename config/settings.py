"""Application settings loaded from environment variables."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Empirically measured with all-MiniLM-L6-v2 cosine distance: on-topic
# questions (direct or loosely worded) against a small business-policy
# corpus landed at 0.21-0.66; clearly off-topic questions landed at
# 0.91-1.0. 0.7 sits in the gap, rejecting off-topic retrieval while still
# accepting on-topic questions phrased differently from the source text.
DEFAULT_RETRIEVAL_MAX_DISTANCE = 0.7
CALLBACK_PATH = "/api/v1/auth/callback"
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9._:/-]{1,64}")
ALLOWED_OIDC_SIGNING_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


@dataclass(frozen=True)
class Settings:
    app_data_dir: Path
    sqlite_db_path: Path
    chroma_persist_dir: Path
    gemini_api_key: str | None
    gemini_model: str
    embedding_model: str
    tenant_id: str | None = None
    session_id: str | None = None
    max_tabular_upload_bytes: int = 50 * 1024 * 1024
    max_tabular_rows: int = 1_000_000
    max_pdf_upload_bytes: int = 25 * 1024 * 1024
    max_total_pdf_bytes: int = 50 * 1024 * 1024
    max_pdf_pages: int = 500
    max_document_chunks: int = 5_000
    retrieval_top_k: int = 4
    retrieval_max_distance: float | None = DEFAULT_RETRIEVAL_MAX_DISTANCE
    retrieval_hybrid: bool = True
    session_retention_hours: int = 24
    session_cleanup_interval_minutes: int = 15
    sqlite_encryption_key: bytes | None = None
    local_only_mode: bool = False
    gemini_exclude_sample_values: bool = False
    debug_log_raw_content: bool = False
    max_chat_messages: int = 50
    max_chat_dataframes_retained: int = 10
    audit_log_dir: Path | None = None
    sweep_orphaned_workspaces: bool = False
    api_auth_mode: str = "local"
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_allowed_algorithms: tuple[str, ...] = ("RS256",)
    oidc_clock_skew_seconds: int = 60
    oidc_jwks_cache_seconds: int = 300
    oidc_http_timeout_seconds: float = 5.0
    oidc_roles_claim: str = "roles"
    web_origin: str | None = None
    session_max_age_seconds: int = 28_800
    session_idle_timeout_seconds: int = 1_800
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = field(default=None, repr=False)
    oidc_redirect_uri: str | None = None
    oidc_scopes: tuple[str, ...] = ("openid",)
    llm_input_cost_per_million_usd: float | None = None
    llm_output_cost_per_million_usd: float | None = None

    @property
    def oidc_login_configured(self) -> bool:
        return self.oidc_client_id is not None

    @property
    def audit_dir(self) -> Path:
        """Where append-only audit files live (outside every workspace directory)."""
        return self.audit_log_dir or self.app_data_dir / "audit"

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key) and not self.local_only_mode

    def validate_identity_configuration(self) -> None:
        """Reject identity configuration that could silently fall back to local access."""
        if self.api_auth_mode not in {"local", "oidc"}:
            raise ValueError("API_AUTH_MODE must be either 'local' or 'oidc'.")

        oidc_values = (self.oidc_issuer_url, self.oidc_audience, self.oidc_jwks_url)
        if self.api_auth_mode == "local":
            if any(
                value is not None
                for value in (
                    *oidc_values,
                    self.web_origin,
                    *self._login_values(),
                    self.oidc_client_secret,
                )
            ):
                raise ValueError(
                    "OIDC settings require API_AUTH_MODE=oidc; refusing a silent local fallback."
                )
            return

        missing = [
            name
            for name, value in (
                ("OIDC_ISSUER_URL", self.oidc_issuer_url),
                ("OIDC_AUDIENCE", self.oidc_audience),
                ("OIDC_JWKS_URL", self.oidc_jwks_url),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise ValueError(f"OIDC mode requires: {', '.join(missing)}.")
        assert self.oidc_issuer_url is not None
        assert self.oidc_jwks_url is not None
        for name, value in (
            ("OIDC_ISSUER_URL", self.oidc_issuer_url),
            ("OIDC_JWKS_URL", self.oidc_jwks_url),
        ):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None:
                raise ValueError(f"{name} must be an HTTPS URL without embedded credentials.")
            if name == "OIDC_ISSUER_URL" and (parsed.query or parsed.fragment):
                raise ValueError("OIDC_ISSUER_URL cannot contain a query string or fragment.")
        if not self.oidc_allowed_algorithms:
            raise ValueError("OIDC_ALLOWED_ALGORITHMS must contain at least one algorithm.")
        unsupported = set(self.oidc_allowed_algorithms) - ALLOWED_OIDC_SIGNING_ALGORITHMS
        if unsupported:
            raise ValueError(
                "OIDC_ALLOWED_ALGORITHMS contains unsupported values: "
                + ", ".join(sorted(unsupported))
            )
        if self.oidc_clock_skew_seconds < 0:
            raise ValueError("OIDC_CLOCK_SKEW_SECONDS cannot be negative.")
        if self.oidc_jwks_cache_seconds < 1:
            raise ValueError("OIDC_JWKS_CACHE_SECONDS must be at least 1.")
        if self.oidc_http_timeout_seconds <= 0:
            raise ValueError("OIDC_HTTP_TIMEOUT_SECONDS must be greater than zero.")
        if not self.oidc_roles_claim.strip() or len(self.oidc_roles_claim) > 64:
            raise ValueError("OIDC_ROLES_CLAIM must be a claim name of 1 to 64 characters.")
        if self.web_origin is not None:
            _validate_web_origin(self.web_origin)
        self._validate_login_configuration()
        if self.session_idle_timeout_seconds < 60:
            raise ValueError("API_SESSION_IDLE_TIMEOUT_SECONDS must be at least 60.")
        if self.session_max_age_seconds < self.session_idle_timeout_seconds:
            raise ValueError(
                "API_SESSION_MAX_AGE_SECONDS must not be shorter than the idle timeout."
            )

    def validate_llm_pricing(self) -> None:
        """Cost estimates need both prices; partial or absurd values are rejected."""
        prices = (self.llm_input_cost_per_million_usd, self.llm_output_cost_per_million_usd)
        if all(price is None for price in prices):
            return
        if any(price is None for price in prices):
            raise ValueError(
                "LLM_INPUT_COST_PER_MILLION_USD and LLM_OUTPUT_COST_PER_MILLION_USD must be "
                "set together."
            )
        if any(
            not math.isfinite(price) or price < 0 or price > 100_000
            for price in prices
            if price is not None
        ):
            raise ValueError("LLM cost per million tokens must be between 0 and 100000 USD.")

    def _login_values(self) -> tuple[str | None, ...]:
        return (
            self.oidc_authorization_endpoint,
            self.oidc_token_endpoint,
            self.oidc_client_id,
            self.oidc_redirect_uri,
        )

    def _validate_login_configuration(self) -> None:
        """Browser login is all-or-nothing; partial configuration fails startup."""
        if all(value is None for value in self._login_values()):
            if self.oidc_client_secret is not None:
                raise ValueError("OIDC_CLIENT_SECRET requires the browser login settings.")
            return
        missing = [
            name
            for name, value in (
                ("OIDC_AUTHORIZATION_ENDPOINT", self.oidc_authorization_endpoint),
                ("OIDC_TOKEN_ENDPOINT", self.oidc_token_endpoint),
                ("OIDC_CLIENT_ID", self.oidc_client_id),
                ("OIDC_REDIRECT_URI", self.oidc_redirect_uri),
                ("WEB_ORIGIN", self.web_origin),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise ValueError(f"Browser login requires: {', '.join(missing)}.")
        assert self.oidc_authorization_endpoint and self.oidc_token_endpoint
        assert self.oidc_redirect_uri and self.web_origin and self.oidc_client_id
        for name, value in (
            ("OIDC_AUTHORIZATION_ENDPOINT", self.oidc_authorization_endpoint),
            ("OIDC_TOKEN_ENDPOINT", self.oidc_token_endpoint),
        ):
            parsed = urlparse(value)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.fragment
            ):
                raise ValueError(f"{name} must be an HTTPS URL without credentials or fragment.")
        redirect = urlparse(self.oidc_redirect_uri)
        origin = urlparse(self.web_origin)
        if (
            (redirect.scheme, redirect.netloc) != (origin.scheme, origin.netloc)
            or redirect.path != CALLBACK_PATH
            or redirect.query
            or redirect.fragment
        ):
            raise ValueError(
                f"OIDC_REDIRECT_URI must be WEB_ORIGIN plus {CALLBACK_PATH}, "
                "with no query or fragment."
            )
        if "openid" not in self.oidc_scopes or any(
            not _SCOPE_PATTERN.fullmatch(scope) for scope in self.oidc_scopes
        ):
            raise ValueError("OIDC_SCOPES must include 'openid' and contain only scope tokens.")
        if len(self.oidc_client_id) > 256 or (
            self.oidc_client_secret is not None and not self.oidc_client_secret.strip()
        ):
            raise ValueError("OIDC_CLIENT_ID or OIDC_CLIENT_SECRET is invalid.")

    @property
    def session_dir(self) -> Path | None:
        if self.tenant_id is None or self.session_id is None:
            return None
        return self.app_data_dir / "sessions" / self.tenant_id / self.session_id

    def for_session(self, tenant_id: str, session_id: str) -> Settings:
        """Return storage settings isolated to one tenant's application session."""
        if not re.fullmatch(r"[a-f0-9]{32}", tenant_id):
            raise ValueError("tenant_id must be a 32-character hexadecimal identifier.")
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id must be a 32-character hexadecimal identifier.")

        session_dir = self.app_data_dir / "sessions" / tenant_id / session_id
        return replace(
            self,
            sqlite_db_path=session_dir / "sqlite" / "app.db",
            chroma_persist_dir=session_dir / "vectorstore",
            tenant_id=tenant_id,
            session_id=session_id,
        )


def get_settings() -> Settings:
    data_dir = Path(os.getenv("APP_DATA_DIR", "data"))

    settings = Settings(
        app_data_dir=data_dir,
        sqlite_db_path=Path(os.getenv("SQLITE_DB_PATH", data_dir / "sqlite" / "app.db")),
        chroma_persist_dir=Path(os.getenv("CHROMA_PERSIST_DIR", data_dir / "vectorstore")),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        max_tabular_upload_bytes=_positive_int("MAX_TABULAR_UPLOAD_BYTES", 50 * 1024 * 1024),
        max_tabular_rows=_positive_int("MAX_TABULAR_ROWS", 1_000_000),
        max_pdf_upload_bytes=_positive_int("MAX_PDF_UPLOAD_BYTES", 25 * 1024 * 1024),
        max_total_pdf_bytes=_positive_int("MAX_TOTAL_PDF_BYTES", 50 * 1024 * 1024),
        max_pdf_pages=_positive_int("MAX_PDF_PAGES", 500),
        max_document_chunks=_positive_int("MAX_DOCUMENT_CHUNKS", 5_000),
        retrieval_top_k=_positive_int("RETRIEVAL_TOP_K", 4),
        retrieval_hybrid=os.getenv("RETRIEVAL_HYBRID", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        retrieval_max_distance=_optional_float(
            "RETRIEVAL_MAX_DISTANCE", DEFAULT_RETRIEVAL_MAX_DISTANCE
        ),
        session_retention_hours=_positive_int("SESSION_RETENTION_HOURS", 24),
        session_cleanup_interval_minutes=_positive_int("SESSION_CLEANUP_INTERVAL_MINUTES", 15),
        sqlite_encryption_key=_optional_hex_key("APP_ENCRYPTION_KEY", byte_length=32),
        local_only_mode=_flag("LOCAL_ONLY_MODE"),
        gemini_exclude_sample_values=_flag("GEMINI_EXCLUDE_SAMPLE_VALUES"),
        debug_log_raw_content=_flag("DEBUG_LOG_RAW_CONTENT"),
        max_chat_messages=_positive_int("MAX_CHAT_MESSAGES", 50),
        max_chat_dataframes_retained=_non_negative_int("MAX_CHAT_DATAFRAMES_RETAINED", 10),
        audit_log_dir=_optional_path("AUDIT_LOG_DIR"),
        sweep_orphaned_workspaces=_flag("SWEEP_ORPHANED_WORKSPACES"),
        api_auth_mode=os.getenv("API_AUTH_MODE", "local").strip().lower(),
        oidc_issuer_url=_optional_text("OIDC_ISSUER_URL"),
        oidc_audience=_optional_text("OIDC_AUDIENCE"),
        oidc_jwks_url=_optional_text("OIDC_JWKS_URL"),
        oidc_allowed_algorithms=_csv_values("OIDC_ALLOWED_ALGORITHMS", ("RS256",)),
        oidc_clock_skew_seconds=_non_negative_int("OIDC_CLOCK_SKEW_SECONDS", 60),
        oidc_jwks_cache_seconds=_positive_int("OIDC_JWKS_CACHE_SECONDS", 300),
        oidc_http_timeout_seconds=_positive_float("OIDC_HTTP_TIMEOUT_SECONDS", 5.0),
        oidc_roles_claim=os.getenv("OIDC_ROLES_CLAIM", "roles").strip() or "roles",
        web_origin=_optional_text("WEB_ORIGIN"),
        session_max_age_seconds=_positive_int("API_SESSION_MAX_AGE_SECONDS", 28_800),
        session_idle_timeout_seconds=_positive_int("API_SESSION_IDLE_TIMEOUT_SECONDS", 1_800),
        oidc_authorization_endpoint=_optional_text("OIDC_AUTHORIZATION_ENDPOINT"),
        oidc_token_endpoint=_optional_text("OIDC_TOKEN_ENDPOINT"),
        oidc_client_id=_optional_text("OIDC_CLIENT_ID"),
        oidc_client_secret=_optional_text("OIDC_CLIENT_SECRET"),
        oidc_redirect_uri=_optional_text("OIDC_REDIRECT_URI"),
        oidc_scopes=_scopes("OIDC_SCOPES"),
        llm_input_cost_per_million_usd=_optional_float("LLM_INPUT_COST_PER_MILLION_USD", None),
        llm_output_cost_per_million_usd=_optional_float("LLM_OUTPUT_COST_PER_MILLION_USD", None),
    )
    settings.validate_identity_configuration()
    settings.validate_llm_pricing()
    return settings


def _validate_web_origin(value: str) -> None:
    """Accept only a bare HTTPS origin; plain HTTP is allowed solely for loopback development."""
    parsed = urlparse(value)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    scheme_ok = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
    if (
        not scheme_ok
        or not parsed.netloc
        or parsed.username is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "WEB_ORIGIN must be a bare HTTPS origin (HTTP only for loopback), "
            "without a path, query, fragment, or credentials."
        )


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be at least 1.")
    return value


def _non_negative_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value


def _optional_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if value < 0:
        raise ValueError(f"{name} cannot be negative.")
    return value


def _optional_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


def _optional_text(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _scopes(name: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return ("openid",)
    return tuple(raw.split())


def _csv_values(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"true", "1", "yes"}


def _optional_hex_key(name: str, *, byte_length: int) -> bytes | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a hexadecimal string.") from exc
    if len(value) != byte_length:
        raise ValueError(f"{name} must decode to exactly {byte_length} bytes.")
    return value
