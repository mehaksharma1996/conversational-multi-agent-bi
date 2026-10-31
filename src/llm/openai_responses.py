"""Canonical OpenAI Responses requests and exact input-token counting (PROD-1 #198, S1).

This is groundwork for the bounded adapter in ``src/llm/bounded.py``. It is **not** an adapter:
nothing here subclasses ``BoundedModelAdapter``, generates text, or is wired into the API, so every
investigation model call stays ``metering_unbounded``. No model snapshot is reviewed
(``REVIEWED_MODEL_PROFILES`` is empty), and nothing here has been checked against the live
provider.

**Canonical request.** ``CanonicalResponsesRequest`` is a frozen, re-validated value built only
from trusted server configuration (a ``ResponsesModelProfile``) plus the rendered prompt. The
prompt is a single user message, so it cannot replace the trusted ``instructions``, the model, the
output cap, or the reasoning setting. Tools, streaming, and storage are not representable:
``tools`` must be empty, and ``store`` is always ``false``. Prompt-bearing fields are hidden from
``repr``.

**One source for both payloads.** ``count_payload`` and ``generation_payload`` both start from
``_input_fields``, so the two cannot drift apart:

- sent to both, token-affecting: ``model``, ``instructions``, ``input``, ``text.format`` (a JSON
  schema is part of the context), ``reasoning`` (when the profile allows it), and
  ``truncation="disabled"`` (``auto`` could drop input and break count equality);
- generation only, not accepted by ``/responses/input_tokens``: ``max_output_tokens``, ``store``,
  ``background``, ``temperature``. They do not change the input count, so changing them does not
  invalidate a count.

**Count binding.** ``ResponsesInputTokenCounter.count`` returns an ``InputTokenCount`` bound to a
keyed fingerprint (HMAC-SHA256, with a per-process random key) of the canonical count payload.
``generation_payload`` is the only way to obtain a generation body, and it needs a count issued by
this module for exactly the same token-affecting fields. Any change, including the model,
invalidates the count (``request_changed``). The fingerprint never leaves the process: it is not
exported, logged, or sent as telemetry, and the key cannot be recovered from it.

**Failure taxonomy** (``TokenCountFailure``, fixed codes, fail closed, no retry, no local
tokenizer or character-length estimate):

- transport: ``not_configured``, ``not_sent``, ``timeout``, ``outcome_unknown``,
  ``redirect_rejected``, ``provider_error`` (any non-200, including an unknown model),
  ``response_too_large``, and ``malformed_response``;
- answer: ``ambiguous_response`` (wrong ``object`` or unexpected fields) and ``invalid_count``
  (missing, non-integer, boolean, or below 1);
- policy: ``input_over_bound`` and ``request_invalid`` (a bypassed or forged request);
- binding: ``request_changed``.

Each error carries the transport ``delivery``. Whether a count request is billable has not been
verified (no live call is authorized), so S2 must not treat a count as free.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, Strict, StringConstraints, model_validator

from src.llm.responses_transport import (
    INPUT_TOKENS_PATH,
    Delivery,
    ResponsesTransport,
    ResponsesTransportError,
    TransportFailure,
    canonical_json,
)

OPENAI_PROVIDER = "openai"
OPENAI_DATA_RECIPIENT = "OpenAI"
MAX_PROFILE_OUTPUT_TOKENS = 32_768
MAX_INPUT_CHARACTERS = 200_000  # a payload-size guard only; never a token estimate
MAX_INSTRUCTION_CHARACTERS = 8_000
MAX_SCHEMA_BYTES = 16_384
COUNT_OBJECT = "response.input_tokens"

# A pinned, dated snapshot such as ``<family>-YYYY-MM-DD``; an alias like ``gpt-4.1-mini`` can move.
_SNAPSHOT = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,80}-\d{4}-\d{2}-\d{2}$")
_SCHEMA_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

ReasoningEffort = Literal["none", "minimal", "low"]
OutputFormat = Literal["text", "json_schema"]


class ResponsesRequestError(ValueError):
    """An unsupported or unsafe request combination; the message is a fixed code."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResponsesModelProfile(_Frozen):
    """Trusted, server-side facts about one pinned snapshot; never derived from a request.

    ``reasoning_efforts`` lists the efforts the reviewed snapshot accepts; empty means a model
    without a reasoning parameter, so none may be sent. ``accepts_temperature`` is false for
    reasoning models. ``evidence`` stays ``unverified`` until a live review (S6) is approved.
    """

    model: Annotated[str, StringConstraints(pattern=_SNAPSHOT.pattern)]
    max_output_tokens: Annotated[int, Strict(), Field(ge=1, le=MAX_PROFILE_OUTPUT_TOKENS)]
    reasoning_efforts: frozenset[ReasoningEffort] = frozenset()
    accepts_temperature: Annotated[bool, Strict()] = False
    evidence: Literal["unverified"] = "unverified"


# No snapshot has reviewed evidence (PROD-1 D2 unresolved), so no production profile exists.
REVIEWED_MODEL_PROFILES: Mapping[str, ResponsesModelProfile] = MappingProxyType({})


class CanonicalResponsesRequest(_Frozen):
    """One validated Responses request; see the module docstring. Build with ``build_request``."""

    profile: ResponsesModelProfile
    instructions: Annotated[
        str, StringConstraints(min_length=1, max_length=MAX_INSTRUCTION_CHARACTERS)
    ] = Field(repr=False)
    input_text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_INPUT_CHARACTERS)] = (
        Field(repr=False)
    )
    output_format: OutputFormat = "text"
    schema_name: str | None = None
    schema_text: str | None = Field(default=None, repr=False)
    reasoning_effort: ReasoningEffort | None = None
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    max_output_tokens: Annotated[int, Strict(), Field(ge=1)]
    background: Annotated[bool, Strict()] = True
    store: Literal[False] = False

    @property
    def model(self) -> str:
        return self.profile.model

    @model_validator(mode="after")
    def _supported_combination(self) -> CanonicalResponsesRequest:
        profile = self.profile
        if not self.input_text.strip() or not self.instructions.strip():
            raise ValueError("empty_content")
        if self.max_output_tokens > profile.max_output_tokens:
            raise ValueError("output_cap_above_profile")
        if profile.reasoning_efforts:
            if self.reasoning_effort not in profile.reasoning_efforts:
                raise ValueError("reasoning_effort_required")
        elif self.reasoning_effort is not None:
            raise ValueError("reasoning_not_supported")
        if self.temperature is not None and not profile.accepts_temperature:
            raise ValueError("temperature_not_supported")
        if self.output_format == "text":
            if self.schema_name is not None or self.schema_text is not None:
                raise ValueError("schema_without_json_schema_format")
        else:
            if self.schema_name is None or not _SCHEMA_NAME.match(self.schema_name):
                raise ValueError("schema_name_invalid")
            if self.schema_text is None or len(self.schema_text.encode()) > MAX_SCHEMA_BYTES:
                raise ValueError("schema_invalid")
            schema = json.loads(self.schema_text)
            if not isinstance(schema, dict) or self.schema_text != _canonical_schema(schema):
                raise ValueError("schema_not_canonical")
        return self


def _canonical_schema(schema: Mapping[str, Any]) -> str:
    return canonical_json(schema).decode("ascii")


def build_request(
    profile: ResponsesModelProfile,
    *,
    instructions: str,
    input_text: str,
    max_output_tokens: int,
    output_format: OutputFormat = "text",
    json_schema: tuple[str, Mapping[str, Any]] | None = None,
    reasoning_effort: ReasoningEffort | None = None,
    temperature: float | None = None,
    tools: Sequence[object] = (),
    background: bool = True,
) -> CanonicalResponsesRequest:
    """Validate one request from trusted ``profile`` and ``instructions`` plus the prompt.

    ``tools`` exists only to refuse them: investigation calls run no provider-side tools.
    """
    if type(profile) is not ResponsesModelProfile:
        raise ResponsesRequestError("profile_invalid")
    if tools:
        raise ResponsesRequestError("tools_not_supported")
    name: str | None = None
    schema_text: str | None = None
    if json_schema is not None:
        name, schema = json_schema
        if not isinstance(schema, Mapping):
            raise ResponsesRequestError("schema_invalid")
        try:
            schema_text = _canonical_schema(schema)
        except (TypeError, ValueError):
            raise ResponsesRequestError("schema_invalid") from None
    try:
        return CanonicalResponsesRequest(
            profile=profile,
            instructions=instructions,
            input_text=input_text,
            output_format=output_format,
            schema_name=name,
            schema_text=schema_text,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            background=background,
        )
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise ResponsesRequestError(_first_code(exc)) from None


def _first_code(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for error in errors():
            message = str(error.get("msg", ""))
            code = message.removeprefix("Value error, ")
            if re.fullmatch(r"[a-z_]{1,64}", code):
                return code
    return "request_invalid"


def _input_fields(request: CanonicalResponsesRequest) -> dict[str, Any]:
    """Every field that can change the input count; the shared root of both payloads."""
    text_format: dict[str, Any] = {"type": "text"}
    if request.output_format == "json_schema" and request.schema_text is not None:
        text_format = {
            "type": "json_schema",
            "name": request.schema_name,
            "schema": json.loads(request.schema_text),
            "strict": True,
        }
    body: dict[str, Any] = {
        "model": request.model,
        "instructions": request.instructions,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": request.input_text}],
            }
        ],
        "text": {"format": text_format},
        "truncation": "disabled",
    }
    if request.reasoning_effort is not None:
        body["reasoning"] = {"effort": request.reasoning_effort}
    return body


def count_payload(request: CanonicalResponsesRequest) -> dict[str, Any]:
    """The ``/responses/input_tokens`` body: exactly the token-affecting fields."""
    return _input_fields(_revalidated(request))


# Per-process key: fingerprints are comparable only inside this process and reveal nothing.
_FINGERPRINT_KEY = secrets.token_bytes(32)
_ISSUER = object()


def input_fingerprint(request: CanonicalResponsesRequest) -> bytes:
    """Keyed digest of the canonical count payload; in-process comparison only."""
    return hmac.new(
        _FINGERPRINT_KEY, canonical_json(count_payload(request)), hashlib.sha256
    ).digest()


@dataclass(frozen=True)
class InputTokenCount:
    """A provider-reported exact count bound to one request's token-affecting fields."""

    model: str
    input_tokens: int
    fingerprint: bytes = field(repr=False)
    _issuer: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise TypeError("InputTokenCount is issued only by ResponsesInputTokenCounter.")


class TokenCountFailure(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NOT_SENT = "not_sent"
    TIMEOUT = "timeout"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REDIRECT_REJECTED = "redirect_rejected"
    PROVIDER_ERROR = "provider_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    MALFORMED_RESPONSE = "malformed_response"
    AMBIGUOUS_RESPONSE = "ambiguous_response"
    INVALID_COUNT = "invalid_count"
    INPUT_OVER_BOUND = "input_over_bound"
    REQUEST_INVALID = "request_invalid"
    REQUEST_CHANGED = "request_changed"


_FROM_TRANSPORT = {
    TransportFailure.NOT_CONFIGURED: TokenCountFailure.NOT_CONFIGURED,
    TransportFailure.NOT_SENT: TokenCountFailure.NOT_SENT,
    TransportFailure.TIMEOUT: TokenCountFailure.TIMEOUT,
    TransportFailure.OUTCOME_UNKNOWN: TokenCountFailure.OUTCOME_UNKNOWN,
    TransportFailure.REDIRECT_REJECTED: TokenCountFailure.REDIRECT_REJECTED,
    TransportFailure.HTTP_STATUS: TokenCountFailure.PROVIDER_ERROR,
    TransportFailure.RESPONSE_TOO_LARGE: TokenCountFailure.RESPONSE_TOO_LARGE,
    TransportFailure.MALFORMED_RESPONSE: TokenCountFailure.MALFORMED_RESPONSE,
}


class TokenCountError(Exception):
    """Counting failed closed. The message is the code alone; nothing may be generated."""

    def __init__(self, failure: TokenCountFailure, *, delivery: Delivery) -> None:
        self.failure = TokenCountFailure(failure)
        self.delivery = Delivery(delivery)
        super().__init__(self.failure.value)


def _revalidated(request: object) -> CanonicalResponsesRequest:
    """Re-check a request so a ``model_construct`` bypass or forged subclass fails closed."""
    if type(request) is not CanonicalResponsesRequest:
        raise TokenCountError(TokenCountFailure.REQUEST_INVALID, delivery=Delivery.NOT_SENT)
    try:
        return CanonicalResponsesRequest.model_validate(request.model_dump(warnings=False))
    except (ValueError, TypeError, AttributeError):
        raise TokenCountError(
            TokenCountFailure.REQUEST_INVALID, delivery=Delivery.NOT_SENT
        ) from None


class ResponsesInputTokenCounter:
    """Exact input counts from ``/responses/input_tokens``; one request per count, no retry."""

    def __init__(
        self, transport: ResponsesTransport, *, max_input_tokens: int, timeout_seconds: float
    ) -> None:
        if type(transport) is not ResponsesTransport:
            raise TypeError("transport must be a ResponsesTransport.")
        if type(max_input_tokens) is not int or max_input_tokens < 1:
            raise ValueError("max_input_tokens must be a positive integer.")
        self._transport = transport
        self._max_input_tokens = max_input_tokens
        self._timeout_seconds = timeout_seconds

    def count(
        self, request: CanonicalResponsesRequest, *, timeout_seconds: float | None = None
    ) -> InputTokenCount:
        """``timeout_seconds`` (the caller's deadline, S3) can only shorten the configured one."""
        payload = count_payload(request)
        timeout = self._timeout_seconds
        if timeout_seconds is not None:
            timeout = min(timeout, timeout_seconds)
        try:
            document = self._transport.post_json(
                INPUT_TOKENS_PATH, payload, timeout_seconds=timeout
            )
        except ResponsesTransportError as error:
            raise TokenCountError(_FROM_TRANSPORT[error.failure], delivery=error.delivery) from None
        if set(document) != {"object", "input_tokens"} or document["object"] != COUNT_OBJECT:
            raise TokenCountError(TokenCountFailure.AMBIGUOUS_RESPONSE, delivery=Delivery.RECEIVED)
        tokens = document["input_tokens"]
        if type(tokens) is not int or tokens < 1:
            raise TokenCountError(TokenCountFailure.INVALID_COUNT, delivery=Delivery.RECEIVED)
        if tokens > self._max_input_tokens:
            raise TokenCountError(TokenCountFailure.INPUT_OVER_BOUND, delivery=Delivery.RECEIVED)
        return InputTokenCount(
            model=request.model,
            input_tokens=tokens,
            fingerprint=hmac.new(
                _FINGERPRINT_KEY, canonical_json(payload), hashlib.sha256
            ).digest(),
            _issuer=_ISSUER,
        )


def generation_payload(
    request: CanonicalResponsesRequest, count: InputTokenCount
) -> dict[str, Any]:
    """The create body, available only with a valid count for this exact input (see module doc).

    S1 builds the body only; nothing here sends it. S2's adapter is the sole intended caller.
    """
    body = count_payload(request)
    if (
        type(count) is not InputTokenCount
        or count._issuer is not _ISSUER
        or count.model != request.model
        or not hmac.compare_digest(count.fingerprint, input_fingerprint(request))
    ):
        raise TokenCountError(TokenCountFailure.REQUEST_CHANGED, delivery=Delivery.NOT_SENT)
    body["max_output_tokens"] = request.max_output_tokens
    body["store"] = False
    body["background"] = request.background
    if request.temperature is not None:
        body["temperature"] = request.temperature
    return body
