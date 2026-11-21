"""PROD-1 S6 smoke tooling (#198), offline only.

Every test drives ``scripts.prod1_live_smoke`` against a scripted ``httpx.MockTransport`` and an
injected clock. Nothing here opens a network connection or reads a real key: a run that reached
the default transport would fail ``test_no_test_reaches_the_default_transport``'s guard.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import prod1_live_smoke as smoke
from src.llm.responses_transport import ResponsesTransport

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "offline-test-placeholder-not-a-credential"
SNAPSHOT = "test-model-2099-01-01"
START = 1_000.0
PRICING = ["--input-usd-per-million", "0.10", "--output-usd-per-million", "0.40"]
SOURCE = ["--price-source", "https://example.invalid/pricing", "--price-accessed", "2026-11-01"]


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds


@dataclass
class FakeOpenAI:
    """A scripted Responses API: exact counts, background generation, provider cancellation."""

    clock: Clock
    count_offset: int = 0  # billed input minus counted input
    served_model: str = SNAPSHOT
    create_error: Exception | None = None
    usage_on_cancel: bool = False
    model_requests: list[str] = field(default_factory=list)  # counts and creates only
    all_requests: list[tuple[str, str]] = field(default_factory=list)
    _runs: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def tokens(body: dict[str, Any]) -> int:
        text = json.dumps([body.get("instructions"), body.get("input")], ensure_ascii=False)
        return max(1, len(text.encode("utf-8")) // 4)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.all_requests.append((request.method, path))
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if path == "/responses/input_tokens":
            self.model_requests.append("count")
            return httpx.Response(
                200, json={"object": "response.input_tokens", "input_tokens": self.tokens(body)}
            )
        if request.method == "POST" and path == "/responses":
            self.model_requests.append("create")
            if self.create_error is not None:
                raise self.create_error
            rid = f"resp_fake{len(self._runs) + 1}"
            self._runs[rid] = {"body": body, "started": self.clock(), "cancelled": False}
            return httpx.Response(200, json=self._doc(rid, "queued"))
        match = re.fullmatch(r"/responses/(resp_[A-Za-z0-9]+)(/cancel)?", path)
        assert match is not None, path
        rid = match.group(1)
        run = self._runs[rid]
        if match.group(2):
            run["cancelled"] = True
        if run["cancelled"]:
            usage = self._usage(run, 7) if self.usage_on_cancel else None
            return httpx.Response(200, json=self._doc(rid, "cancelled", usage=usage))
        cap = run["body"]["max_output_tokens"]
        duration = 0.5 if cap <= 32 else 1.0 if cap <= 64 else 60.0
        if self.clock() - run["started"] < duration:
            return httpx.Response(200, json=self._doc(rid, "in_progress"))
        if cap <= 32:
            return httpx.Response(
                200, json=self._doc(rid, "completed", usage=self._usage(run, 2), text="OK")
            )
        return httpx.Response(
            200,
            json=self._doc(
                rid,
                "incomplete",
                usage=self._usage(run, cap),
                text="1 2 3",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        )

    def _usage(self, run: dict[str, Any], output: int) -> dict[str, int]:
        return {
            "input_tokens": self.tokens(run["body"]) + self.count_offset,
            "output_tokens": output,
        }

    def _doc(
        self,
        rid: str,
        status: str,
        *,
        usage: dict[str, int] | None = None,
        text: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "object": "response",
            "id": rid,
            "model": self.served_model,
            "status": status,
            "background": True,
            **extra,
        }
        if usage is not None:
            document["usage"] = usage
        if text is not None:
            document["output"] = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        return document


@pytest.fixture(autouse=True)
def _no_default_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any request through a real ``httpx.HTTPTransport`` fails the test instead of leaving."""

    def refuse(*_: object, **__: object) -> httpx.Response:
        raise AssertionError("a test reached the network transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


def _transport(provider: FakeOpenAI) -> ResponsesTransport:
    return ResponsesTransport(
        FAKE_KEY, transport=httpx.MockTransport(provider), clock=provider.clock
    )


def _pricing() -> smoke.Pricing:
    from datetime import date

    return smoke.Pricing(
        Decimal("0.10"), Decimal("0.40"), "https://example.invalid/p", date(2026, 11, 1)
    )


def _run(provider: FakeOpenAI, guard: smoke.SpendGuard, **kwargs: Any) -> dict[str, Any]:
    return smoke.run_smoke(
        model=SNAPSHOT,
        transport=_transport(provider),
        pricing=_pricing(),
        guard=guard,
        clock=provider.clock,
        sleep=provider.clock.sleep,
        **kwargs,
    )


def _live_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--model",
        SNAPSHOT,
        *PRICING,
        *SOURCE,
        "--max-usd",
        "1.00",
        "--confirm-spend",
        "--output",
        str(tmp_path / "s6.json"),
        *extra,
    ]


def _main(
    argv: list[str],
    *,
    environ: dict[str, str] | None = None,
    factory: Callable[[str], ResponsesTransport] | None = None,
    clock: Clock | None = None,
) -> int:
    def never(_: str) -> ResponsesTransport:
        raise AssertionError("no transport may be built before every guard passes")

    fake = clock or Clock()
    return smoke.main(
        argv,
        environ={"OPENAI_API_KEY": FAKE_KEY} if environ is None else environ,
        transport_factory=factory or never,
        git_state=lambda: ("0" * 40, False),
        clock=fake,
        sleep=fake.sleep,
    )


# --- the probe set ---------------------------------------------------------------------------


def test_the_probe_set_covers_the_brief_s6_evidence() -> None:
    by_category = {c: [p for p in smoke.PROBES if p.category == c] for c in ("count", "cap")}
    counts = by_category["count"]

    assert len(counts) >= 20  # brief section 5: N >= 20 varied prompts
    assert any(not p.prompt.isascii() for p in counts)
    assert any(unicodedata.normalize("NFKC", p.prompt) != p.prompt for p in counts)
    assert {p.category for p in smoke.PROBES} == {"count", "cap", "cancel", "deadline"}
    assert len({p.id for p in smoke.PROBES}) == len(smoke.PROBES)
    for probe in smoke.PROBES:
        assert re.fullmatch(r"[a-z0-9-]{1,40}", probe.id)
        assert 16 <= probe.max_output_tokens <= smoke.PROFILE_MAX_OUTPUT_TOKENS
        assert smoke.upper_bound_input_tokens(probe.prompt) <= smoke.PROFILE_MAX_INPUT_TOKENS
    assert re.fullmatch(r"[0-9a-f]{64}", smoke.probe_set_digest())
    assert smoke.probe_set_digest() == smoke.probe_set_digest(smoke.PROBES)


def test_the_planned_worst_case_fits_the_hard_caps() -> None:
    worst, requests = smoke.plan_worst_case(smoke.PROBES, _pricing())

    assert requests == 2 * len(smoke.PROBES) <= smoke.HARD_MAX_REQUESTS
    assert worst < Decimal("0.05")  # at these illustrative rates


# --- a complete scripted run -----------------------------------------------------------------


def test_a_scripted_run_meets_every_expectation_and_writes_a_content_free_artifact(
    tmp_path: Path,
) -> None:
    provider = FakeOpenAI(Clock())

    code = _main(
        _live_args(tmp_path), factory=lambda key: _transport(provider), clock=provider.clock
    )

    assert code == 0
    text = (tmp_path / "s6.json").read_text(encoding="utf-8")
    artifact = json.loads(text)
    summary = artifact["summary"]
    assert artifact["evidence_kind"] == "measured"
    assert artifact["reviewed"] is False and artifact["pricing"]["reviewed"] is False
    assert artifact["run"]["candidate_review_label"] == smoke.CANDIDATE_REVIEW
    assert artifact["run"]["probe_set_sha256"] == smoke.probe_set_digest()
    assert summary["aborted"] is None
    assert summary["count_probes"] == summary["count_equal"] == len(smoke.COUNT_SAMPLES)
    assert summary["expectations_met"] == len(smoke.PROBES)
    assert summary["requests_sent"] == 2 * len(smoke.PROBES)
    assert summary["served_models"] == [SNAPSHOT]
    assert summary["latency_ms"]["n"] == len(smoke.COUNT_SAMPLES)
    assert summary["cancel_to_terminal_ms"]["n"] == 1
    assert provider.model_requests.count("create") == len(smoke.PROBES)  # one create per probe
    assert artifact["limitations"]
    # Content-free: no prompt, answer, response ID, instructions, or key.
    for forbidden in (FAKE_KEY, "resp_fake", smoke.INSTRUCTIONS[:40], "Résumé", "ﬁnance", "1 2 3"):
        assert forbidden not in text
    for probe in smoke.PROBES:
        assert probe.prompt not in text


def test_a_count_mismatch_is_recorded_not_hidden() -> None:
    provider = FakeOpenAI(Clock(), count_offset=1)

    summary = _run(provider, smoke.SpendGuard(Decimal("1"), 80))

    assert summary["aborted"] is None
    assert summary["count_equal"] == 0
    assert summary["count_mismatch"] == len(smoke.COUNT_SAMPLES)
    count_records = [r for r in summary["records"] if r["category"] == "count"]
    assert all(r["expectation_met"] is False for r in count_records)


def test_cancel_and_deadline_need_provider_confirmation() -> None:
    provider = FakeOpenAI(Clock())

    records = {r["id"]: r for r in _run(provider, smoke.SpendGuard(Decimal("1"), 80))["records"]}

    cancel, deadline = records["cancel-mid-generation"], records["deadline-short"]
    assert cancel["termination"] == "provider_cancelled" and cancel["termination_confirmed"]
    assert deadline["termination"] == "provider_deadline_enforced"
    assert deadline["termination_confirmed"] and deadline["expectation_met"]
    assert cancel["usage_input_tokens"] is None  # no usage reported: the reservation stays charged
    cap = records["cap-forced-long"]
    assert cap["provider_status"] == "incomplete" and cap["usage_output_tokens"] == 64


def test_a_served_identity_mismatch_is_recorded_as_a_failure() -> None:
    provider = FakeOpenAI(Clock(), served_model="other-model-2099-02-02")

    summary = _run(provider, smoke.SpendGuard(Decimal("1"), 80), probes=smoke.PROBES[:1])

    record = summary["records"][0]
    assert record["failure"] == "identity_mismatch"
    assert record["usage_input_tokens"] is None  # stopped at the first foreign document
    assert record["expectation_met"] is False
    assert summary["expectations_met"] == 0
    assert summary["served_models"] == ["other-model-2099-02-02"]


# --- spend and safety guards -----------------------------------------------------------------


def test_the_ceiling_stops_the_run_before_a_request_would_cross_it() -> None:
    provider = FakeOpenAI(Clock())
    pricing = _pricing()
    first = smoke.PROBES[0]
    per_probe = pricing.cost(smoke.upper_bound_input_tokens(first.prompt), first.max_output_tokens)
    ceiling = per_probe * Decimal("2.5")
    guard = smoke.SpendGuard(ceiling, 80)

    summary = _run(provider, guard)

    assert summary["aborted"] == "spend_ceiling_reached"
    assert guard.committed <= ceiling
    assert len(provider.model_requests) == guard.requests < 2 * len(smoke.PROBES)


def test_the_request_cap_counts_model_requests_only() -> None:
    provider = FakeOpenAI(Clock())
    guard = smoke.SpendGuard(Decimal("1"), 3)

    summary = _run(provider, guard)

    assert summary["aborted"] == "request_cap_reached"
    assert len(provider.model_requests) == 3
    assert len(provider.all_requests) > 3  # polls are not model attempts


def test_an_uncertain_create_keeps_its_reservation_and_ends_the_run() -> None:
    provider = FakeOpenAI(Clock(), create_error=httpx.ReadTimeout("timed out"))
    guard = smoke.SpendGuard(Decimal("1"), 80)

    summary = _run(provider, guard)

    assert summary["aborted"] == "uncertain_outcome"
    assert provider.model_requests == ["count", "create"]  # never resent, nothing after
    assert summary["records"][0]["failure"] in {"create_outcome_unknown", "create_rejected"}
    assert guard.committed > 0


@pytest.mark.parametrize(
    ("argv_change", "environ", "code"),
    [
        (lambda a: [x for x in a if x != "--confirm-spend"], None, "confirm_spend_required"),
        (lambda a: a, {"OPENAI_API_KEY": FAKE_KEY, "CI": "true"}, "ci_environment"),
        (lambda a: a, {"OPENAI_API_KEY": FAKE_KEY, "GITHUB_ACTIONS": "true"}, "ci_environment"),
        (lambda a: a, {}, "key_missing"),
        (
            lambda a: [x if x != SNAPSHOT else "gpt-latest" for x in a],
            None,
            "model_snapshot_required",
        ),
        (lambda a: [x for x in a if x not in SOURCE], None, "pricing_incomplete"),
        (lambda a: [x if x != "1.00" else "5.01" for x in a], None, "max_usd_out_of_range"),
        (lambda a: [x if x != "1.00" else "0.000001" for x in a], None, "plan_exceeds_ceiling"),
        (lambda a: [*a, "--max-requests", "81"], None, "max_requests_out_of_range"),
        (lambda a: [*a, "--max-requests", "4"], None, "plan_exceeds_request_cap"),
    ],
)
def test_a_live_run_is_refused_before_any_transport_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv_change: Callable[[list[str]], list[str]],
    environ: dict[str, str] | None,
    code: str,
) -> None:
    assert _main(argv_change(_live_args(tmp_path)), environ=environ) == 2
    assert f"Refused: {code}" in capsys.readouterr().err
    assert not (tmp_path / "s6.json").exists()


def test_an_existing_artifact_is_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "s6.json").write_text("{}", encoding="utf-8")

    assert _main(_live_args(tmp_path)) == 2
    assert "Refused: output_exists" in capsys.readouterr().err
    assert (tmp_path / "s6.json").read_text(encoding="utf-8") == "{}"


def test_the_plan_needs_no_key_and_opens_no_connection(capsys: pytest.CaptureFixture[str]) -> None:
    assert _main(["--plan", *PRICING, *SOURCE], environ={}) == 0
    out = capsys.readouterr().out
    assert f"{len(smoke.PROBES)} probes" in out and "worst case" in out


def test_entered_prices_are_validated() -> None:
    from datetime import date

    with pytest.raises(smoke.SmokeRefused, match="price_source_invalid"):
        smoke.Pricing(Decimal("1"), Decimal("1"), "http://example.invalid", date(2026, 1, 1))
    with pytest.raises(smoke.SmokeRefused, match="price_invalid"):
        smoke.Pricing(Decimal("0"), Decimal("1"), "https://example.invalid", date(2026, 1, 1))
    with pytest.raises(smoke.SmokeRefused, match="price_accessed_in_future"):
        smoke.Pricing(Decimal("1"), Decimal("1"), "https://example.invalid", date(2999, 1, 1))


# --- boundaries ------------------------------------------------------------------------------


def test_only_the_smoke_script_drives_the_adapter_and_nothing_imports_the_script() -> None:
    scripts = [
        path.name
        for path in (ROOT / "scripts").rglob("*.py")
        if "openai_responses_bounded" in path.read_text(encoding="utf-8")
    ]
    importers = [
        path.relative_to(ROOT).as_posix()
        for folder in ("src", "apps", "packages", "config", "scripts")
        for path in (ROOT / folder).rglob("*.py")
        if "prod1_live_smoke" in path.read_text(encoding="utf-8")
        and path.name != "prod1_live_smoke.py"
    ]

    assert scripts == ["prod1_live_smoke.py"]
    assert importers == []


def test_the_smoke_admits_nothing() -> None:
    from apps.api import investigation_providers
    from src.llm.openai_responses import REVIEWED_MODEL_PROFILES
    from src.llm.openai_responses_bounded import REVIEWED_CAPABILITY_PROFILES

    tree = ast.parse((ROOT / "scripts" / "prod1_live_smoke.py").read_text(encoding="utf-8"))
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    used |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not used & {
        "REVIEWED_CAPABILITY_PROFILES",
        "REVIEWED_MODEL_PROFILES",
        "LIVE_ADMISSION_RELEASED",
        "load_dotenv",
        "dotenv",
    }
    assert dict(REVIEWED_CAPABILITY_PROFILES) == {} and dict(REVIEWED_MODEL_PROFILES) == {}
    assert investigation_providers.LIVE_ADMISSION_RELEASED is False
