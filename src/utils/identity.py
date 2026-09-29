"""Tenant identity derivation for per-user storage isolation."""

from __future__ import annotations

from src.utils.hashing import sha256_bytes


def derive_tenant_id(identity: str) -> str:
    """Derive a stable, path-safe tenant id from an authenticated identity."""
    return sha256_bytes(identity.encode("utf-8"))[:32]


LOCAL_DEV_TENANT_ID = derive_tenant_id("local-unauthenticated")
