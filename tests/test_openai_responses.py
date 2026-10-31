"""PROD-1 S1: canonical Responses requests, exact counting, and the transport seam (offline).

Every test uses ``httpx.MockTransport``; no API key, network, or provider account is needed.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.investigation.gateway import (
    PLANNING_STAGE_OUTPUT_TOKENS,
    MeteringUnbounded,
    ProviderAttemptBound,
    ProviderEnvelope,
)
from src.llm import openai_responses as responses
from src.llm import responses_transport as transport_module
from src.llm.bounded import BoundedModelAdapter, admit_planning_adapter
from src.llm.openai_responses import (
    REVIEWED_MODEL_PROFILES,
    CanonicalResponsesRequest,
    InputTokenCount,
    ResponsesInputTokenCounter,
    ResponsesModelProfile,
    ResponsesRequestError,
    TokenCountError,
    TokenCountFailure,
    build_request,
    count_payload,
    generation_payload,
    input_fingerprint,
)
from src.llm.responses_transport import (
    INPUT_TOKENS_PATH,
    OPENAI_API_BASE,
    Delivery,
    ResponsesTransport,
    ResponsesTransportError,
    TransportFailure,
    canonical_json,
)
from src.orchestration.tool_gateway import admitted_model_adapter

# Deliberately not key-shaped: it cannot authenticate anywhere, and only mock transports see it.
FAKE_KEY = "offline-test-placeholder-not-a-credential"
SNAPSHOT = "test-model-2099-01-01"
PROMPT = "Explain churn for SEGMENT-SECRET-7731"
INSTRUCTIONS = "Follow the application task exactly."
PROVIDER_TEXT = "internal provider detail: account 4242"


def _profile(**changes: Any) -> ResponsesModelProfile:
    values: dict[str, Any] = {"model": SNAPSHOT, "max_output_tokens": 4_000}
    values.update(changes)
    return ResponsesModelProfile(**values)


def _request(**changes: Any) -> CanonicalResponsesRequest:
    values: dict[str, Any] = {
        "instructions": INSTRUCTIONS,
        "input_text": PROMPT,
        "max_output_tokens": PLANNING_STAGE_OUTPUT_TOKENS,
    }
    profile = changes.pop("profile", _profile())
    values.update(changes)
    return build_request(profile, **values)


class Wire:
    """Scripted transport recording every request; only this ever answers in these tests."""

    def __init__(self, *responses_: httpx.Response | Exception) -> None:
        self.responses = list(responses_)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _count_response(tokens: object = 42, **extra: object) -> httpx.Response:
    return httpx.Response(
        200, json={"object": "response.input_tokens", "input_tokens": tokens, **extra}
    )


def _counter(
    wire: Wire, *, key: str | None = FAKE_KEY, max_input_tokens: int = 6_000, **kwargs: Any
) -> ResponsesInputTokenCounter:
    transport = ResponsesTransport(key, transport=wire.transport, **kwargs)
    return ResponsesInputTokenCounter(
        transport, max_input_tokens=max_input_tokens, timeout_seconds=5.0
    )


def _count_error(
    wire: Wire, request: CanonicalResponsesRequest | None = None, **kwargs: Any
) -> TokenCountError:
    with pytest.raises(TokenCountError) as caught:
        _counter(wire, **kwargs).count(request or _request())
    return caught.value


# --- 1-3: canonical construction, stable identity, count/generation parity -------------------


def test_valid_request_builds_both_payloads_from_one_canonical_value() -> None:
    request = _request()
    wire = Wire(_count_response(57))
    count = _counter(wire).count(request)

    sent = json.loads(wire.requests[0].content)
    assert wire.requests[0].url == OPENAI_API_BASE + INPUT_TOKENS_PATH
    assert sent == count_payload(request)
    assert sent == {
        "model": SNAPSHOT,
        "instructions": INSTRUCTIONS,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]}
        ],
        "text": {"format": {"type": "text"}},
        "truncation": "disabled",
    }
    assert count.input_tokens == 57 and count.model == SNAPSHOT

    body = generation_payload(request, count)
    generation_only = {"max_output_tokens", "store", "background"}
    assert set(body) - set(sent) == generation_only
    assert {key: body[key] for key in sent} == sent  # every token-affecting field is identical
    assert body["store"] is False and body["background"] is True
    assert body["max_output_tokens"] == PLANNING_STAGE_OUTPUT_TOKENS


def test_count_payload_never_carries_generation_only_fields() -> None:
    request = _request(profile=_profile(accepts_temperature=True), temperature=0.0)
    payload = count_payload(request)
    for generation_only in ("max_output_tokens", "store", "background", "temperature", "stream"):
        assert generation_only not in payload


def test_serialization_and_identity_are_stable() -> None:
    schema_a = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    schema_b = {"properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "type": "object"}
    first = _request(output_format="json_schema", json_schema=("plan", schema_a))
    second = _request(output_format="json_schema", json_schema=("plan", schema_b))
    assert first == second
    assert canonical_json(count_payload(first)) == canonical_json(count_payload(second))
    assert input_fingerprint(first) == input_fingerprint(second)
    assert count_payload(first)["text"]["format"] == {
        "type": "json_schema",
        "name": "plan",
        "schema": schema_a,
        "strict": True,
    }


def test_canonical_request_is_immutable() -> None:
    request = _request()
    with pytest.raises(ValueError):
        request.input_text = "something else"


# --- 4: token-affecting mutations invalidate a count; generation-only ones do not ----------


@pytest.mark.parametrize(
    "change",
    [
        {"input_text": PROMPT + " "},
        {"instructions": INSTRUCTIONS + "!"},
        {"profile": _profile(model="test-model-2099-01-02")},
        {"output_format": "json_schema", "json_schema": ("plan", {"type": "object"})},
        {
            "profile": _profile(reasoning_efforts=frozenset({"low", "minimal"})),
            "reasoning_effort": "low",
        },
    ],
)
def test_a_token_affecting_change_invalidates_the_count(change: dict[str, Any]) -> None:
    count = _counter(Wire(_count_response())).count(_request())
    changed = _request(**change)
    with pytest.raises(TokenCountError) as caught:
        generation_payload(changed, count)
    assert caught.value.failure is TokenCountFailure.REQUEST_CHANGED
    assert caught.value.delivery is Delivery.NOT_SENT


def test_reasoning_effort_change_invalidates_the_count() -> None:
    profile = _profile(reasoning_efforts=frozenset({"low", "minimal"}))
    count = _counter(Wire(_count_response())).count(
        _request(profile=profile, reasoning_effort="low")
    )
    with pytest.raises(TokenCountError):
        generation_payload(_request(profile=profile, reasoning_effort="minimal"), count)


def test_generation_only_changes_keep_the_count_valid() -> None:
    profile = _profile(accepts_temperature=True)
    count = _counter(Wire(_count_response())).count(_request(profile=profile))
    changed = _request(profile=profile, max_output_tokens=100, background=False, temperature=0.5)
    body = generation_payload(changed, count)
    assert body["max_output_tokens"] == 100 and body["background"] is False
    assert body["temperature"] == 0.5


# --- 5-7: unsupported models, tools, formats, reasoning, and output caps -------------------


@pytest.mark.parametrize(
    "model", ["gpt-4.1-mini", "latest", "Test-Model-2099-01-01", "m-99-1-1", ""]
)
def test_an_undated_or_malformed_model_is_not_a_profile(model: str) -> None:
    with pytest.raises(ValueError):
        _profile(model=model)


def test_no_snapshot_is_reviewed_so_no_production_profile_exists() -> None:
    assert dict(REVIEWED_MODEL_PROFILES) == {}
    assert all(profile.evidence == "unverified" for profile in REVIEWED_MODEL_PROFILES.values())


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"tools": [{"type": "web_search"}]}, "tools_not_supported"),
        ({"output_format": "json_schema"}, "schema_name_invalid"),
        (
            {"output_format": "json_schema", "json_schema": ("bad name!", {"type": "object"})},
            "schema_name_invalid",
        ),
        ({"json_schema": ("plan", {"type": "object"})}, "schema_without_json_schema_format"),
        ({"output_format": "yaml"}, "request_invalid"),
        ({"reasoning_effort": "low"}, "reasoning_not_supported"),
        ({"temperature": 0.2}, "temperature_not_supported"),
        ({"max_output_tokens": 4_001}, "output_cap_above_profile"),
        ({"max_output_tokens": 0}, "request_invalid"),
        ({"max_output_tokens": True}, "request_invalid"),
        ({"input_text": "   "}, "empty_content"),
        ({"instructions": ""}, "request_invalid"),
    ],
)
def test_unsupported_or_unsafe_combinations_are_refused(changes: dict[str, Any], code: str) -> None:
    with pytest.raises(ResponsesRequestError) as caught:
        _request(**changes)
    assert str(caught.value) == code
    assert PROMPT not in str(caught.value)


def test_a_reasoning_profile_requires_an_allowed_effort() -> None:
    profile = _profile(reasoning_efforts=frozenset({"minimal"}))
    for effort in (None, "low"):
        with pytest.raises(ResponsesRequestError) as caught:
            _request(profile=profile, reasoning_effort=effort)
        assert str(caught.value) == "reasoning_effort_required"
    assert count_payload(_request(profile=profile, reasoning_effort="minimal"))["reasoning"] == {
        "effort": "minimal"
    }


def test_store_cannot_be_enabled_and_extra_fields_are_refused() -> None:
    request = _request()
    with pytest.raises(ValueError):
        CanonicalResponsesRequest.model_validate({**request.model_dump(), "store": True})
    with pytest.raises(ValueError):
        CanonicalResponsesRequest.model_validate({**request.model_dump(), "tools": []})


def test_a_bypassed_request_fails_closed_before_any_io() -> None:
    forged = CanonicalResponsesRequest.model_construct(
        **{**_request().model_dump(), "max_output_tokens": 999_999}
    )
    wire = Wire(_count_response())
    error = _count_error(wire, forged)
    assert (
        error.failure is TokenCountFailure.REQUEST_INVALID and error.delivery is Delivery.NOT_SENT
    )
    assert wire.requests == []


# --- 8-12: count failures fail closed with fixed codes ------------------------------------


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        ({"object": "response.input_tokens"}, TokenCountFailure.AMBIGUOUS_RESPONSE),
        ({"input_tokens": 3}, TokenCountFailure.AMBIGUOUS_RESPONSE),
        ({"object": "response", "input_tokens": 3}, TokenCountFailure.AMBIGUOUS_RESPONSE),
        (
            {"object": "response.input_tokens", "input_tokens": 3, "estimate": True},
            TokenCountFailure.AMBIGUOUS_RESPONSE,
        ),
        (
            {"object": "response.input_tokens", "input_tokens": None},
            TokenCountFailure.INVALID_COUNT,
        ),
        ({"object": "response.input_tokens", "input_tokens": -1}, TokenCountFailure.INVALID_COUNT),
        ({"object": "response.input_tokens", "input_tokens": 0}, TokenCountFailure.INVALID_COUNT),
        (
            {"object": "response.input_tokens", "input_tokens": 12.0},
            TokenCountFailure.INVALID_COUNT,
        ),
        (
            {"object": "response.input_tokens", "input_tokens": "12"},
            TokenCountFailure.INVALID_COUNT,
        ),
        (
            {"object": "response.input_tokens", "input_tokens": True},
            TokenCountFailure.INVALID_COUNT,
        ),
        (
            {"object": "response.input_tokens", "input_tokens": 6_001},
            TokenCountFailure.INPUT_OVER_BOUND,
        ),
    ],
)
def test_missing_invalid_or_ambiguous_counts_fail_closed(
    body: dict[str, Any], failure: TokenCountFailure
) -> None:
    error = _count_error(Wire(httpx.Response(200, json=body)))
    assert error.failure is failure and error.delivery is Delivery.RECEIVED


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'{"object": "response.input_tokens", "input_tokens": NaN}',
        b'{"input_tokens": 1, "input_tokens": 2, "object": "response.input_tokens"}',
        b"\xff\xfe",
    ],
)
def test_malformed_responses_fail_closed(raw: bytes) -> None:
    error = _count_error(Wire(httpx.Response(200, content=raw)))
    assert error.failure is TokenCountFailure.MALFORMED_RESPONSE


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
def test_provider_errors_fail_closed_without_provider_text_or_retry(status: int) -> None:
    wire = Wire(httpx.Response(status, text=PROVIDER_TEXT), _count_response())
    error = _count_error(wire)
    assert error.failure is TokenCountFailure.PROVIDER_ERROR and error.delivery is Delivery.RECEIVED
    assert str(error) == "provider_error" and PROVIDER_TEXT not in repr(error)
    assert len(wire.requests) == 1  # no retry


@pytest.mark.parametrize(
    ("raised", "failure", "delivery"),
    [
        (httpx.ConnectError("refused"), TokenCountFailure.NOT_SENT, Delivery.NOT_SENT),
        (httpx.ConnectTimeout("slow connect"), TokenCountFailure.NOT_SENT, Delivery.NOT_SENT),
        (httpx.ReadTimeout("slow read"), TokenCountFailure.TIMEOUT, Delivery.UNKNOWN),
        (httpx.WriteTimeout("slow write"), TokenCountFailure.TIMEOUT, Delivery.UNKNOWN),
        (httpx.RemoteProtocolError("reset"), TokenCountFailure.OUTCOME_UNKNOWN, Delivery.UNKNOWN),
        (httpx.ReadError(PROVIDER_TEXT), TokenCountFailure.OUTCOME_UNKNOWN, Delivery.UNKNOWN),
    ],
)
def test_network_faults_say_whether_the_request_may_have_been_delivered(
    raised: Exception, failure: TokenCountFailure, delivery: Delivery
) -> None:
    wire = Wire(raised, _count_response())
    error = _count_error(wire)
    assert (error.failure, error.delivery) == (failure, delivery)
    assert len(wire.requests) == 1 and PROVIDER_TEXT not in str(error)
    assert error.__cause__ is None and error.__suppress_context__


def test_a_body_streaming_past_the_total_deadline_is_a_timeout() -> None:
    ticks = iter([0.0, 10.0])
    wire = Wire(_count_response())
    error = _count_error(wire, clock=lambda: next(ticks))
    assert error.failure is TokenCountFailure.TIMEOUT and error.delivery is Delivery.RECEIVED


def test_an_oversized_response_is_refused() -> None:
    wire = Wire(_count_response(padding="x" * 200))
    error = _count_error(wire, max_response_bytes=128)
    assert error.failure is TokenCountFailure.RESPONSE_TOO_LARGE


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_rejected_and_never_followed(status: int) -> None:
    wire = Wire(
        httpx.Response(status, headers={"location": "https://attacker.example/steal"}),
        _count_response(),
    )
    error = _count_error(wire)
    assert error.failure is TokenCountFailure.REDIRECT_REJECTED
    assert [str(r.url) for r in wire.requests] == [OPENAI_API_BASE + INPUT_TOKENS_PATH]


def test_transport_refuses_unlisted_paths_and_unbounded_timeouts() -> None:
    transport = ResponsesTransport(FAKE_KEY, transport=Wire(_count_response()).transport)
    with pytest.raises(ValueError):
        transport.post_json("/responses", {}, timeout_seconds=1.0)  # create is S2, not S1
    for timeout in (0, -1, 31, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            transport.post_json(INPUT_TOKENS_PATH, {}, timeout_seconds=timeout)


# --- 13-16: secrets and content never reach diagnostics or logs ---------------------------


def test_missing_key_is_not_configured_before_anything_is_sent() -> None:
    wire = Wire(_count_response())
    error = _count_error(wire, key=None)
    assert error.failure is TokenCountFailure.NOT_CONFIGURED and error.delivery is Delivery.NOT_SENT
    assert wire.requests == []


def test_the_key_appears_only_in_the_authorization_header() -> None:
    wire = Wire(httpx.Response(500, text=FAKE_KEY))
    transport = ResponsesTransport(FAKE_KEY, transport=wire.transport)
    assert FAKE_KEY not in repr(transport) and FAKE_KEY not in str(vars(transport).keys())
    with pytest.raises(ResponsesTransportError) as caught:
        transport.post_json(INPUT_TOKENS_PATH, count_payload(_request()), timeout_seconds=1.0)
    assert FAKE_KEY not in str(caught.value) and FAKE_KEY not in repr(caught.value)
    sent = wire.requests[0]
    assert sent.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in sent.content.decode()
    assert FAKE_KEY not in str(sent.url)


def test_reprs_and_errors_never_show_prompt_instructions_or_counts_fingerprint() -> None:
    request = _request(
        output_format="json_schema", json_schema=("plan", {"title": "SCHEMA-SECRET"})
    )
    count = _counter(Wire(_count_response())).count(request)
    for text in (repr(request), str(request), repr(count)):
        assert PROMPT not in text and INSTRUCTIONS not in text and "SCHEMA-SECRET" not in text
    assert count.fingerprint.hex() not in repr(count)


def test_nothing_is_logged_on_success_or_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _counter(Wire(_count_response())).count(_request())
    with pytest.raises(TokenCountError):
        _counter(Wire(httpx.Response(500, text=PROVIDER_TEXT))).count(_request())
    for record in caplog.records:
        message = record.getMessage()
        assert PROMPT not in message and PROVIDER_TEXT not in message and FAKE_KEY not in message
    assert not [r for r in caplog.records if r.name.startswith("src.llm")]


def test_no_generation_body_without_a_valid_count() -> None:
    request = _request()
    with pytest.raises(TokenCountError):
        _counter(Wire(httpx.Response(503))).count(request)
    with pytest.raises(TypeError):
        InputTokenCount(model=SNAPSHOT, input_tokens=10, fingerprint=input_fingerprint(request))
    forged = object.__new__(InputTokenCount)
    object.__setattr__(forged, "model", SNAPSHOT)
    object.__setattr__(forged, "input_tokens", 10)
    object.__setattr__(forged, "fingerprint", input_fingerprint(request))
    object.__setattr__(forged, "_issuer", None)
    for bad in (forged, None, object()):
        with pytest.raises(TokenCountError) as caught:
            generation_payload(request, bad)  # type: ignore[arg-type]
        assert caught.value.failure is TokenCountFailure.REQUEST_CHANGED


# --- 17-18: inert import and construction; no key or network needed ------------------------


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield attempts


def test_construction_and_unconfigured_count_open_no_connection(no_sockets: list[object]) -> None:
    transport = transport_module.ResponsesTransport(None)
    responses.ResponsesInputTokenCounter(transport, max_input_tokens=6_000, timeout_seconds=1.0)
    with pytest.raises(responses.TokenCountError):  # no key: refused before any socket
        responses.ResponsesInputTokenCounter(
            transport, max_input_tokens=6_000, timeout_seconds=1.0
        ).count(
            responses.build_request(
                responses.ResponsesModelProfile(model=SNAPSHOT, max_output_tokens=10),
                instructions=INSTRUCTIONS,
                input_text=PROMPT,
                max_output_tokens=10,
            )
        )
    assert no_sockets == []


def test_modules_import_in_a_clean_interpreter_without_keys() -> None:
    root = str(Path(__file__).resolve().parents[1])
    code = "\n".join(
        [
            "import socket, sys",
            "sys.path.insert(0, sys.argv[1])",
            "def refuse(*args, **kwargs):",
            "    raise SystemExit('network access attempted')",
            "socket.socket.connect = socket.create_connection = socket.getaddrinfo = refuse",
            "import src.llm.openai_responses as r, src.llm.responses_transport as t",
            "r.ResponsesInputTokenCounter(",
            "    t.ResponsesTransport(None), max_input_tokens=1, timeout_seconds=1.0",
            ")",
        ]
    )
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH") if key in os.environ}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, root],
        capture_output=True,
        text=True,
        env=env,  # no provider keys, no dotenv
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]


# --- 19-20: existing interfaces and fail-closed admission are unchanged --------------------


def test_the_counter_is_not_an_adapter_and_admission_still_fails_closed() -> None:
    counter = _counter(Wire(_count_response()))
    member = ProviderAttemptBound(
        provider="openai",
        model=SNAPSHOT,
        data_recipient="OpenAI",
        max_attempts=1,
        max_output_tokens=PLANNING_STAGE_OUTPUT_TOKENS,
        input_bounded=True,
    )
    envelope = ProviderEnvelope(members=(member,))
    assert not isinstance(counter, BoundedModelAdapter)
    assert admitted_model_adapter(counter, envelope) is None
    with pytest.raises(MeteringUnbounded):
        admit_planning_adapter(counter, envelope=envelope, capability=None)


def test_production_investigation_adapters_still_fail_closed() -> None:
    from apps.api.investigations import FailClosedAdapters

    planning = FailClosedAdapters().planning()
    execution = FailClosedAdapters().execution()
    assert planning.adapter is None and planning.evidence is None and planning.pricing is None
    assert execution.llm_client is None and execution.envelope.members == ()


def test_existing_provider_factory_knows_nothing_about_openai() -> None:
    from src.llm import factory

    text = Path(factory.__file__).read_text(encoding="utf-8")
    assert "openai" not in text.lower()


def test_transport_error_message_is_a_fixed_code() -> None:
    error = ResponsesTransportError(
        TransportFailure.HTTP_STATUS, delivery=Delivery.RECEIVED, status=500
    )
    assert str(error) == "http_status:500"
