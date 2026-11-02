"""Versioned consent notices and which recipients each version discloses (PROD-1 S4, S5d, #198).

A workspace's consent records the notice version it accepted and the recipients the server
configuration named at that moment. Most recipients are disclosed by every notice version (the
existing ``2026-09`` behaviour, unchanged). A recipient listed in ``_DISCLOSING_VERSIONS`` is
disclosed only by the versions named there, so consent to any other version never covers it:

- the consent route records such a recipient only when the accepted version discloses it;
- ``effective_recipients`` drops it from a stored consent whose version does not disclose it, so
  an older, stale, or hand-edited record cannot authorise it either.

OpenAI is such a recipient. No data migration is needed: an existing row (``2026-09``, with or
without ``OpenAI`` in its recipients) reads as "OpenAI not granted", and the workspace must accept
``OPENAI_NOTICE_VERSION`` explicitly. Consent is necessary, never sufficient: it admits no
provider, and nothing admits one today (``apps.api.investigation_providers``).

``effective_recipients`` is the one reading of a stored consent: an unrecognised version, a
withdrawal, or a record that is both accepted and withdrawn grants nothing at all. Withdrawal
applies to the whole workspace record, every recipient at once, because that is the record every
check reads.

The notice text is server-owned and versioned with this module. Changing what a released notice
says requires a new version, and removing a version from ``_DISCLOSING_VERSIONS`` makes every
consent to it stale. ``2026-09`` was previously web-client copy; it is served from here with the
same disclosure. ``2026-10-openai-1`` was never released or accepted outside tests before S5d.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from src.llm.openai_responses import OPENAI_DATA_RECIPIENT

BASE_NOTICE_VERSION = "2026-09"
OPENAI_NOTICE_VERSION = "2026-10-openai-1"

ConsentStatus = Literal["not_required", "missing", "current", "stale", "withdrawn"]


@dataclass(frozen=True)
class NoticeSection:
    heading: str
    body: str


@dataclass(frozen=True)
class ConsentNotice:
    version: str
    title: str
    # The body of the notice's own disclosure; ``sections`` is the complete text people read.
    text: str
    sections: tuple[NoticeSection, ...]


@dataclass(frozen=True)
class ServedNotice:
    """A notice plus the recipients this server configuration would record on accepting it."""

    notice: ConsentNotice
    recipients: tuple[str, ...]
    digest: str


_QUESTIONS_SECTION = NoticeSection(
    "What model-backed questions send",
    "Model-backed questions may send question text, table schema and samples, or retrieved PDF "
    "excerpts to the recipients listed in this notice. Each recipient's own terms govern how it "
    "processes and retains that data.",
)
_WITHDRAWAL_SECTION = NoticeSection(
    "Withdrawing consent",
    "You can withdraw this consent for the whole workspace at any time. Withdrawal covers every "
    "recipient listed in this notice: no new model request starts after a check that sees it, "
    "and questions and investigations that need these recipients are refused until a notice is "
    "accepted again. It does not recall a request already sent, and it does not delete data a "
    "recipient has already received.",
)

BASE_NOTICE = ConsentNotice(
    version=BASE_NOTICE_VERSION,
    title="Hosted model data sharing",
    text=_QUESTIONS_SECTION.body,
    sections=(_QUESTIONS_SECTION, _WITHDRAWAL_SECTION),
)

_OPENAI_TEXT = (
    "Investigations in this workspace may send data to OpenAI, an external processor, through "
    "the OpenAI Responses API. What may be sent: the investigation question; the plan, task "
    "instructions, and prompts built from it; dataset schema details (column names and "
    "types); query results and aggregates from your uploaded tables; and excerpts of your "
    "uploaded documents. Requests are sent with storage disabled (store=false), but they run "
    "in OpenAI's background mode, and OpenAI documents that background responses are kept "
    "for about ten minutes so they can be retrieved. That is OpenAI's documented practice, "
    "not a guaranteed deletion deadline, and OpenAI's own terms govern its processing. "
    "Accepting this notice does not turn on OpenAI processing: an administrator must also "
    "configure and activate a reviewed model, and each investigation still needs its own "
    "approvals and budget."
)

OPENAI_NOTICE = ConsentNotice(
    version=OPENAI_NOTICE_VERSION,
    title="Hosted model and OpenAI data sharing",
    text=_OPENAI_TEXT,
    sections=(
        _QUESTIONS_SECTION,
        NoticeSection("What investigations send to OpenAI", _OPENAI_TEXT),
        _WITHDRAWAL_SECTION,
    ),
)

# Every version this server recognises. Acceptance of anything else is refused.
NOTICES: Mapping[str, ConsentNotice] = MappingProxyType(
    {notice.version: notice for notice in (BASE_NOTICE, OPENAI_NOTICE)}
)

# Recipients disclosed only by specific notice versions. Absent recipients: every version.
_DISCLOSING_VERSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {OPENAI_DATA_RECIPIENT: frozenset({OPENAI_NOTICE_VERSION})}
)

# The only versions a client may accept without a digest: what the web client sent before digests
# existed. An allowlist, so every other version needs one, and it must never name a version that
# discloses a recipient in ``_DISCLOSING_VERSIONS``.
_DIGEST_OPTIONAL_VERSIONS: frozenset[str] = frozenset({BASE_NOTICE_VERSION})

# Audit tokens for recipient names, so a display name never reaches the audit log as free text.
_AUDIT_TOKENS: Mapping[str, str] = MappingProxyType(
    {
        "Gemini": "gemini",
        "Anthropic": "anthropic",
        "Ollama (remote)": "ollama_remote",
        OPENAI_DATA_RECIPIENT: "openai",
    }
)
_UNLISTED_AUDIT_TOKEN = "unlisted"


def digest_required(notice_version: str) -> bool:
    """True unless ``notice_version`` is a legacy version that may be accepted without a digest."""
    return notice_version not in _DIGEST_OPTIONAL_VERSIONS


def discloses(notice_version: str | None, recipient: str) -> bool:
    """True when consent to ``notice_version`` can cover ``recipient``."""
    versions = _DISCLOSING_VERSIONS.get(recipient)
    if versions is None:
        return True
    return notice_version is not None and notice_version in versions


def disclosed_recipients(notice_version: str | None, recipients: Iterable[str]) -> tuple[str, ...]:
    """``recipients`` without any that ``notice_version`` does not disclose, order kept."""
    return tuple(recipient for recipient in recipients if discloses(notice_version, recipient))


def consent_in_force(
    notice_version: str | None, accepted_at: datetime | None, withdrawn_at: datetime | None
) -> bool:
    """True for an acceptance of a recognised notice that has not been withdrawn."""
    return accepted_at is not None and withdrawn_at is None and notice_version in NOTICES


def effective_recipients(
    notice_version: str | None,
    accepted_at: datetime | None,
    withdrawn_at: datetime | None,
    recipients: Iterable[str],
) -> tuple[str, ...]:
    """The recipients a stored consent really authorises: none unless it is in force."""
    if not consent_in_force(notice_version, accepted_at, withdrawn_at):
        return ()
    return disclosed_recipients(notice_version, recipients)


def consent_status(
    required: Collection[str],
    effective: Collection[str],
    accepted_at: datetime | None,
    withdrawn_at: datetime | None,
) -> ConsentStatus:
    """What a person should be told about a workspace's consent under this configuration."""
    if not required:
        return "not_required"
    if accepted_at is None:
        return "withdrawn" if withdrawn_at is not None else "missing"
    return "current" if set(required) <= set(effective) else "stale"


def served_notice(recipients: tuple[str, ...]) -> ServedNotice | None:
    """The notice to show for the configured ``recipients``; ``None`` when nothing is shared."""
    if not recipients:
        return None
    notice = OPENAI_NOTICE if OPENAI_DATA_RECIPIENT in recipients else BASE_NOTICE
    canonical = json.dumps(
        {
            "version": notice.version,
            "title": notice.title,
            "sections": [[section.heading, section.body] for section in notice.sections],
            "recipients": recipients,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ServedNotice(notice=notice, recipients=recipients, digest=digest)


def audit_recipients(recipients: Iterable[str]) -> str | None:
    """A fixed-vocabulary audit token such as ``gemini+openai``; ``None`` for no recipients."""
    tokens = sorted({_AUDIT_TOKENS.get(name, _UNLISTED_AUDIT_TOKEN) for name in recipients})
    return "+".join(tokens) or None


__all__ = [
    "BASE_NOTICE",
    "BASE_NOTICE_VERSION",
    "NOTICES",
    "OPENAI_NOTICE",
    "OPENAI_NOTICE_VERSION",
    "ConsentNotice",
    "ConsentStatus",
    "NoticeSection",
    "ServedNotice",
    "audit_recipients",
    "consent_in_force",
    "consent_status",
    "digest_required",
    "disclosed_recipients",
    "discloses",
    "effective_recipients",
    "served_notice",
]
