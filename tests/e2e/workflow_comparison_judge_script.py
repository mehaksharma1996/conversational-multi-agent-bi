"""Scripted judges for the EVAL-2 (#147) S2 judge mode. NOT a model, and NOT an opinion.

:class:`CannedStructuralJudge` reads only what a real judge would read (the prompt), finds the
packet id and the references in the data block, and answers with one canned, structure-only reply:
every dimension rated 4 citing the first answer reference, except citation support, which is rated 4
citing the first source when sources are shown and is ``insufficient_evidence`` otherwise. It never
reads the answer's meaning, so its ratings say nothing about quality; they exercise the judge
contract, validation, and reporting end to end.

:class:`ReplyJudge` answers with whatever a test supplies (text or an exception), for the contract
and adversarial tests. Every prompt is kept in memory for the leak checks; nothing is written.

Development and test only: nothing in ``src/``, ``apps/``, ``packages/``, or ``scripts/`` may import
it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from packages.evaluation import comparison_judge as cj

JUDGE_IDENTITY = cj.JudgeIdentity(
    kind="scripted",
    provider="scripted-eval2-judge",
    model="scripted-eval2-canned-structural",
    version="1",
)

_DATA = re.compile(r"^DATA-BEGIN-([0-9a-f]{24})\n(.*)\nDATA-END-\1\n?\Z", re.DOTALL | re.MULTILINE)
_PACKET_ID = re.compile(r"^Packet id: ([0-9a-f]{16})$", re.MULTILINE)


def read_prompt(prompt: str) -> tuple[str, dict[str, Any]]:
    """The packet id and the data a prompt carries, read the way a model would see them."""
    packet_id = _PACKET_ID.search(prompt)
    data = _DATA.search(prompt)
    if packet_id is None or data is None:
        raise ValueError("not a judge prompt")
    return packet_id.group(1), json.loads(data.group(2))


def _first(data: dict[str, Any]) -> str:
    sentences = data["answer"]["sentences"]
    if sentences:
        return str(sentences[0]["ref"])
    return str(data["answer"]["tables"][0]["ref"])


def canned_reply(prompt: str) -> str:
    packet_id, data = read_prompt(prompt)
    first = _first(data)
    sources = data["evidence_shown"]["sources"]
    dimensions: dict[str, Any] = {
        name: {"status": cj.RATED, "rating": 4, "issues": [], "evidence": [first]}
        for name in cj.DIMENSION_NAMES
    }
    dimensions["citation_support"] = (
        {"status": cj.RATED, "rating": 4, "issues": [], "evidence": [sources[0]["ref"]]}
        if sources
        else {"status": cj.INSUFFICIENT_EVIDENCE, "rating": None, "issues": [], "evidence": []}
    )
    return json.dumps(
        {
            "rubric_version": cj.RUBRIC_VERSION,
            "packet_id": packet_id,
            "decision": "assessed",
            "dimensions": dimensions,
        }
    )


class CannedStructuralJudge:
    """The runner's scripted judge: one canned, structure-only reply per prompt."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def identity(self) -> cj.JudgeIdentity:
        return JUDGE_IDENTITY

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return canned_reply(prompt)


class ReplyJudge:
    """Answers each prompt with ``reply(prompt)``: a string, or an exception to raise."""

    def __init__(self, reply: Callable[[str], str | BaseException]) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    @property
    def identity(self) -> cj.JudgeIdentity:
        return JUDGE_IDENTITY

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = self._reply(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer
