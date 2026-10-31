"""The HTTP seam for the OpenAI Responses bounded adapter (PROD-1 #198, S1 and S2).

One small class owns every byte that would leave for the provider, so the request builder and the
adapter never touch ``httpx`` directly. Its posture:

- **one fixed endpoint**: ``https://api.openai.com/v1`` and four operations. ``post_json`` reaches
  only ``/responses/input_tokens`` (S1). S2 adds ``create_response`` (``POST /responses``),
  ``retrieve_response`` (``GET /responses/{id}``), and ``cancel_response`` (``POST
  /responses/{id}/cancel``). An ID must be ``resp_`` plus ASCII letters and digits, so no caller
  can build another path, query, or host;
- **no retry, no redirect**: exactly one request per call, ``follow_redirects=False``, and a 3xx
  is ``redirect_rejected``;
- **bounded**: a response-size cap and a total deadline. ``httpx`` timeouts are per phase, so the
  pool, connect, write, and read phases each get a fifth of ``timeout_seconds``, and the total is
  re-checked when the headers arrive and after every body chunk. A request therefore ends within
  ``timeout_seconds`` (four phases, plus at most one read wait after the last check). DNS
  resolution is outside ``httpx`` timeouts and is unverified;
- **explicit delivery**: every failure says whether the request was ``not_sent`` (provably never
  delivered), ``unknown`` (it may have reached the provider), or ``received`` (the provider
  answered). The later adapter must not release budget or send again on ``unknown``;
- **secret-safe**: the key is held privately and never appears in ``repr``, exceptions, or logs.
  Exception messages are fixed codes (plus an HTTP status), never provider text. The module logs
  nothing, and requests go straight to the ``httpx`` transport, so ``httpx.Client``'s INFO request
  line (which would carry the URL and a response ID) is never emitted. Environment proxy settings
  are not read: the request goes only to the fixed origin;
- **inert until called**: construction and import open no connection. A missing key is
  ``not_configured`` at call time, before anything is sent, so offline tests never need one.

Tests inject an ``httpx.MockTransport``; nothing in S1 or S2 constructs a real network transport.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from enum import StrEnum
from typing import Any, TypeGuard

import httpx

OPENAI_API_BASE = "https://api.openai.com/v1"
INPUT_TOKENS_PATH = "/responses/input_tokens"
RESPONSES_PATH = "/responses"
# ``post_json`` only: create, retrieve, and cancel have their own fixed-shape methods.
ALLOWED_PATHS = frozenset({INPUT_TOKENS_PATH})
MAX_RESPONSE_BYTES = 65_536
MAX_TIMEOUT_SECONDS = 30.0
_PHASES = 5  # pool, connect, write, first read, and one read wait after the last deadline check
_RESPONSE_ID = re.compile(r"resp_[A-Za-z0-9]{1,128}")


def valid_response_id(value: object) -> TypeGuard[str]:
    """A provider response ID that is safe in a path: ``resp_`` plus ASCII letters and digits."""
    return isinstance(value, str) and _RESPONSE_ID.fullmatch(value) is not None


class TransportFailure(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NOT_SENT = "not_sent"
    TIMEOUT = "timeout"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REDIRECT_REJECTED = "redirect_rejected"
    HTTP_STATUS = "http_status"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"


class Delivery(StrEnum):
    """What is known about whether the provider saw the request."""

    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    RECEIVED = "received"


class ResponsesTransportError(Exception):
    """A code-only transport failure; never carries provider text, the request, or the key."""

    def __init__(
        self, failure: TransportFailure, *, delivery: Delivery, status: int | None = None
    ) -> None:
        self.failure = TransportFailure(failure)
        self.delivery = Delivery(delivery)
        self.status = status
        super().__init__(self.failure.value if status is None else f"{self.failure.value}:{status}")


def canonical_json(body: Mapping[str, Any]) -> bytes:
    """The one serialization of a request body: sorted keys, no spaces, ASCII, finite numbers."""
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


class ResponsesTransport:
    """POST canonical JSON to an allowlisted Responses path, once; see the module docstring."""

    def __init__(
        self,
        api_key: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes is out of range.")
        self.__api_key = api_key or None
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._clock = clock

    def __repr__(self) -> str:
        return f"ResponsesTransport(configured={self.configured})"

    @property
    def configured(self) -> bool:
        return self.__api_key is not None

    def post_json(
        self, path: str, body: Mapping[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]:
        """One request to ``path``; the JSON object answered, or ``ResponsesTransportError``."""
        if path not in ALLOWED_PATHS:
            raise ValueError("Path is not an allowlisted Responses path.")
        return self._exchange("POST", path, canonical_json(body), timeout_seconds)

    def create_response(self, body: Mapping[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        """``POST /responses`` once. The caller must never resend after a non-``not_sent`` error."""
        return self._exchange("POST", RESPONSES_PATH, canonical_json(body), timeout_seconds)

    def retrieve_response(self, response_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        """``GET /responses/{id}`` for one known ID: reads state, generates nothing."""
        return self._exchange("GET", _response_path(response_id), None, timeout_seconds)

    def cancel_response(self, response_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        """``POST /responses/{id}/cancel`` with no body, for one known ID."""
        return self._exchange(
            "POST", _response_path(response_id) + "/cancel", None, timeout_seconds
        )

    def _exchange(
        self, method: str, path: str, content: bytes | None, timeout_seconds: float
    ) -> dict[str, Any]:
        """Send one request; argument errors are caller bugs, raised before any I/O."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is out of range.")
        if self.__api_key is None:
            raise ResponsesTransportError(
                TransportFailure.NOT_CONFIGURED, delivery=Delivery.NOT_SENT
            )
        headers = {"authorization": f"Bearer {self.__api_key}", "accept": "application/json"}
        if content is not None:
            headers["content-type"] = "application/json"
        # Built and sent below the ``httpx.Client`` layer: a transport never follows redirects, and
        # the client's INFO request line (which would log the URL, and with it a response ID) is
        # never emitted. The timeout travels with the request.
        request = httpx.Request(
            method,
            OPENAI_API_BASE + path,
            headers=headers,
            content=content,
            extensions={"timeout": httpx.Timeout(float(timeout_seconds) / _PHASES).as_dict()},
        )
        deadline = self._clock() + float(timeout_seconds)
        try:
            with self._open() as transport:
                response = transport.handle_request(request)
                try:
                    payload = self._read(response, deadline)
                finally:
                    response.close()
        except ResponsesTransportError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # Raised before any request byte is written: provably not delivered.
            raise ResponsesTransportError(
                TransportFailure.NOT_SENT, delivery=Delivery.NOT_SENT
            ) from None
        except httpx.TimeoutException:
            raise ResponsesTransportError(
                TransportFailure.TIMEOUT, delivery=Delivery.UNKNOWN
            ) from None
        except Exception:  # any other transport fault: the provider may have seen the request
            raise ResponsesTransportError(
                TransportFailure.OUTCOME_UNKNOWN, delivery=Delivery.UNKNOWN
            ) from None
        return _strict_json_object(payload)

    def _open(self) -> AbstractContextManager[httpx.BaseTransport]:
        """The injected transport (left open), or a fresh one closed after this request."""
        if self._transport is not None:
            return nullcontext(self._transport)
        return httpx.HTTPTransport()

    def _read(self, response: httpx.Response, deadline: float) -> bytes:
        status = response.status_code
        if 300 <= status < 400:
            raise ResponsesTransportError(
                TransportFailure.REDIRECT_REJECTED, delivery=Delivery.RECEIVED, status=status
            )
        if status != 200:
            raise ResponsesTransportError(
                TransportFailure.HTTP_STATUS, delivery=Delivery.RECEIVED, status=status
            )
        if self._clock() > deadline:
            raise ResponsesTransportError(TransportFailure.TIMEOUT, delivery=Delivery.RECEIVED)
        payload = bytearray()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > self._max_response_bytes:
                raise ResponsesTransportError(
                    TransportFailure.RESPONSE_TOO_LARGE, delivery=Delivery.RECEIVED
                )
            if self._clock() > deadline:
                raise ResponsesTransportError(TransportFailure.TIMEOUT, delivery=Delivery.RECEIVED)
        return bytes(payload)


def _response_path(response_id: str) -> str:
    if not valid_response_id(response_id):
        raise ValueError("response_id is not a valid Responses ID.")
    return f"{RESPONSES_PATH}/{response_id}"


def _strict_json_object(payload: bytes) -> dict[str, Any]:
    """Decode one JSON object, refusing duplicate keys and non-finite constants."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_: str) -> Any:
        raise ValueError("non-finite constant")

    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ResponsesTransportError(
            TransportFailure.MALFORMED_RESPONSE, delivery=Delivery.RECEIVED
        ) from None
    if not isinstance(document, dict):
        raise ResponsesTransportError(
            TransportFailure.MALFORMED_RESPONSE, delivery=Delivery.RECEIVED
        )
    return document
