"""Versioned consent notices and which recipients each version discloses (PROD-1 S4, #198).

A workspace's consent records the notice version it accepted and the recipients the server
configuration named at that moment. Most recipients are disclosed by every notice version (the
existing ``2026-09`` behaviour, unchanged). A recipient listed in ``_DISCLOSING_VERSIONS`` is
disclosed only by the versions named there, so consent to any other version never covers it:

- the consent route records such a recipient only when the accepted version discloses it;
- the investigation runtime drops it from a stored consent whose version does not disclose it, so
  an older, stale, or hand-edited record cannot authorise it either.

OpenAI is such a recipient. No database migration is needed: an existing row (``2026-09``, with or
without ``OpenAI`` in its recipients) reads as "OpenAI not granted", and the workspace must accept
``OPENAI_NOTICE_VERSION`` explicitly. Consent is necessary, never sufficient: it admits no
provider, and S4 admits none (``apps.api.investigation_providers``).

The notice text is server-owned and versioned with this module. Changing what it says requires a
new version, and removing a version from ``_DISCLOSING_VERSIONS`` makes every consent to it stale.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.llm.openai_responses import OPENAI_DATA_RECIPIENT

BASE_NOTICE_VERSION = "2026-09"
OPENAI_NOTICE_VERSION = "2026-10-openai-1"


@dataclass(frozen=True)
class ConsentNotice:
    version: str
    recipient: str
    text: str


OPENAI_NOTICE = ConsentNotice(
    version=OPENAI_NOTICE_VERSION,
    recipient=OPENAI_DATA_RECIPIENT,
    text=(
        "Investigations in this workspace may send data to OpenAI, an external processor, through "
        "the OpenAI Responses API. What may be sent: the investigation question; the plan, task "
        "instructions, and prompts built from it; dataset schema details (column names and "
        "types); query results and aggregates from your uploaded tables; and excerpts of your "
        "uploaded documents. Requests are sent with storage disabled (store=false), but they run "
        "in OpenAI's background mode, and OpenAI documents that background responses are kept "
        "for about ten minutes so they can be retrieved. That is OpenAI's documented practice, "
        "not a guaranteed deletion deadline, and OpenAI's own terms govern its processing. "
        "Accepting this notice does not turn on OpenAI processing: an administrator must also "
        "configure and activate a reviewed model, each investigation still needs its own "
        "approvals and budget, and you can stop using it by not running investigations."
    ),
)

# Recipients disclosed only by specific notice versions. Absent recipients: every version.
_DISCLOSING_VERSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {OPENAI_DATA_RECIPIENT: frozenset({OPENAI_NOTICE_VERSION})}
)


def discloses(notice_version: str | None, recipient: str) -> bool:
    """True when consent to ``notice_version`` can cover ``recipient``."""
    versions = _DISCLOSING_VERSIONS.get(recipient)
    if versions is None:
        return True
    return notice_version is not None and notice_version in versions


def disclosed_recipients(notice_version: str | None, recipients: Iterable[str]) -> tuple[str, ...]:
    """``recipients`` without any that ``notice_version`` does not disclose, order kept."""
    return tuple(recipient for recipient in recipients if discloses(notice_version, recipient))


__all__ = [
    "BASE_NOTICE_VERSION",
    "OPENAI_NOTICE",
    "OPENAI_NOTICE_VERSION",
    "ConsentNotice",
    "disclosed_recipients",
    "discloses",
]
