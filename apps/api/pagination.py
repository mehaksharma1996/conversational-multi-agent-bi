"""Opaque stable-cursor helpers shared by API collection endpoints."""

from __future__ import annotations

import base64
import json
from binascii import Error as Base64Error
from datetime import datetime

from apps.api.errors import ApiError


def encode_cursor(created_at: datetime, resource_id: str) -> str:
    """Encode a stable ``(created_at, id)`` exclusive cursor."""
    payload = json.dumps(
        [created_at.isoformat(), resource_id],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    """Decode a cursor without exposing its representation in API errors."""
    try:
        padded = value + "=" * (-len(value) % 4)
        created_at_text, resource_id = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None or not isinstance(resource_id, str) or not resource_id:
            raise ValueError
    except (Base64Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApiError(422, "invalid_cursor", "The pagination cursor is invalid.") from None
    return created_at, resource_id
