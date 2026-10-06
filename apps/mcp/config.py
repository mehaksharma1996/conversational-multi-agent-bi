"""Server-side MCP configuration.

Everything that selects *what* the server can see (database, table, document index, tenant) comes
from here, at process start. No tool argument can override any of it, so a host - or a prompt
injected into a host - cannot widen the data scope or pick another tenant.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings, get_settings
from src.documents.vector_store import DEFAULT_COLLECTION_NAME
from src.storage.sqlite_store import DEFAULT_TABLE_NAME, validate_identifier
from src.utils.identity import LOCAL_DEV_TENANT_ID, derive_tenant_id

MAX_ROW_LIMIT = 1_000
MAX_TIMEOUT_SECONDS = 30.0


class McpConfigurationError(ValueError):
    """Raised at startup for missing or unsafe server configuration (fail closed)."""


@dataclass(frozen=True)
class McpConfig:
    settings: Settings
    database_path: Path
    table_name: str
    tenant_id: str
    max_rows: int
    query_timeout_seconds: float
    vectorstore_dir: Path | None
    collection_name: str
    audit_dir: Path


def load_mcp_config(
    environ: Mapping[str, str] | None = None,
    settings: Settings | None = None,
) -> McpConfig:
    env = os.environ if environ is None else environ
    active_settings = settings or get_settings()

    raw_database = env.get("MCP_DATABASE_PATH", "").strip()
    if not raw_database:
        raise McpConfigurationError("MCP_DATABASE_PATH is required.")
    database_path = Path(raw_database).expanduser().resolve()
    if not database_path.is_file():
        raise McpConfigurationError("MCP_DATABASE_PATH must point to an existing SQLite file.")

    try:
        table_name = validate_identifier(env.get("MCP_TABLE_NAME", DEFAULT_TABLE_NAME).strip())
    except ValueError as exc:
        raise McpConfigurationError("MCP_TABLE_NAME is not a valid identifier.") from exc

    subject = env.get("MCP_TENANT_SUBJECT", "").strip()
    tenant_id = derive_tenant_id(subject) if subject else LOCAL_DEV_TENANT_ID

    max_rows = _bounded_int(env, "MCP_MAX_ROWS", default=200, low=1, high=MAX_ROW_LIMIT)
    timeout = _bounded_float(
        env, "MCP_QUERY_TIMEOUT_SECONDS", default=5.0, high=MAX_TIMEOUT_SECONDS
    )

    raw_vectorstore = env.get("MCP_VECTORSTORE_DIR", "").strip()
    vectorstore_dir = Path(raw_vectorstore).expanduser().resolve() if raw_vectorstore else None
    if vectorstore_dir is not None and not vectorstore_dir.is_dir():
        raise McpConfigurationError("MCP_VECTORSTORE_DIR must be an existing directory.")

    # A separate directory from the API's audit files: the hash-chained JSONL sink assumes a
    # single writer per file, and the API may be running at the same time.
    return McpConfig(
        settings=active_settings,
        database_path=database_path,
        table_name=table_name,
        tenant_id=tenant_id,
        max_rows=max_rows,
        query_timeout_seconds=timeout,
        vectorstore_dir=vectorstore_dir,
        collection_name=env.get("MCP_COLLECTION_NAME", DEFAULT_COLLECTION_NAME).strip()
        or DEFAULT_COLLECTION_NAME,
        audit_dir=active_settings.audit_dir / "mcp",
    )


def _bounded_int(env: Mapping[str, str], name: str, *, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise McpConfigurationError(f"{name} must be an integer.") from exc
    if not low <= value <= high:
        raise McpConfigurationError(f"{name} must be between {low} and {high}.")
    return value


def _bounded_float(env: Mapping[str, str], name: str, *, default: float, high: float) -> float:
    raw = env.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError as exc:
        raise McpConfigurationError(f"{name} must be a number.") from exc
    if not 0 < value <= high:
        raise McpConfigurationError(f"{name} must be greater than 0 and at most {high:g}.")
    return value
