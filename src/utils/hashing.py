"""Stable content hashing helpers."""

from __future__ import annotations

from hashlib import sha256


def sha256_bytes(payload: bytes) -> str:
    """Return a hexadecimal SHA-256 digest for uploaded content."""
    return sha256(payload).hexdigest()
