"""Build a user's data-export archive for one workspace (issue #18).

The archive is a ZIP the owner can read without this application: the original uploaded files,
conversation history as JSON with each retained result table as CSV, generated reports as Markdown,
earlier exports, and a manifest describing the workspace, its retention, and what is (and is not)
included. It never contains another tenant's data, secrets, prompts, or model output beyond the
answers the user already saw.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from apps.api.repository import WorkspaceSnapshot
from src.agents.report_agent import report_to_markdown
from src.utils.spreadsheet_safety import sanitize_dataframe_for_export

EXPORT_FORMAT = "workspace-export/1"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceExportTooLargeError(ValueError):
    """The export would exceed the configured size limit."""


def _safe(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip("._") or "file"
    return cleaned[:100]


def _json(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, default=str).encode("utf-8")


def build_workspace_export(
    snapshot: WorkspaceSnapshot, *, retention_hours: int, max_bytes: int
) -> tuple[bytes, int]:
    """Return ``(zip bytes, file count)``; raises if the contents exceed ``max_bytes``."""
    files: dict[str, bytes] = {}
    total = 0

    def add(path: str, payload: bytes) -> None:
        nonlocal total
        total += len(payload)
        if total > max_bytes:
            raise WorkspaceExportTooLargeError("The workspace is too large to export in one file.")
        files[path] = payload

    for upload in snapshot.uploads:
        add(f"uploads/{upload.id}/{_safe(upload.filename)}", upload.payload)

    messages_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for message in snapshot.messages:
        result_file: str | None = None
        if message.dataframe is not None:
            result_file = f"conversations/{message.conversation_id}/{message.id}.csv"
            frame = sanitize_dataframe_for_export(message.dataframe)
            add(result_file, frame.to_csv(index=False).encode("utf-8"))
        messages_by_conversation.setdefault(message.conversation_id, []).append(
            {
                "id": message.id,
                "created_at": message.created_at.isoformat(),
                "question": message.question,
                "answer": message.answer,
                "route": message.route,
                "sql": message.sql,
                "sources": list(message.sources),
                "status": message.status,
                "request_id": message.request_id,
                "result_file": result_file,
            }
        )
    for conversation in snapshot.conversations:
        add(
            f"conversations/{conversation.id}.json",
            _json(
                {
                    "id": conversation.id,
                    "created_at": conversation.created_at.isoformat(),
                    "dataset_id": conversation.dataset_id,
                    "document_collection_id": conversation.document_collection_id,
                    "messages": messages_by_conversation.get(conversation.id, []),
                }
            ),
        )

    for report in snapshot.reports:
        add(f"reports/{report.id}.md", report_to_markdown(report.report).encode("utf-8"))
    for export in snapshot.exports:
        add(f"exports/{export.id}-{_safe(export.filename)}", export.payload)
    if snapshot.document_collections:
        add(
            "documents/collections.json",
            _json(
                [
                    {
                        "id": item.id,
                        "files": list(item.filenames),
                        "page_count": item.page_count,
                        "chunk_count": item.chunk_count,
                        "indexed_at": item.created_at.isoformat(),
                    }
                    for item in snapshot.document_collections
                ]
            ),
        )

    workspace = snapshot.workspace
    manifest = {
        "format": EXPORT_FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "workspace": {
            "id": workspace.id,
            "created_at": workspace.created_at.isoformat(),
            "expires_at": workspace.expires_at.isoformat(),
            "retention_hours": retention_hours,
            "authentication_mode": workspace.authentication_mode,
            "local_only_mode": workspace.local_only_mode,
        },
        "consent": {
            "accepted": workspace.consent_accepted_at is not None,
            "notice_version": workspace.consent_notice_version,
            "accepted_at": (
                workspace.consent_accepted_at.isoformat() if workspace.consent_accepted_at else None
            ),
            "data_recipients": list(workspace.consent_recipients),
        },
        "contents": {
            "uploads": len(snapshot.uploads),
            "datasets": len(snapshot.datasets),
            "conversations": len(snapshot.conversations),
            "messages": len(snapshot.messages),
            "reports": len(snapshot.reports),
            "exports": len(snapshot.exports),
            "document_collections": len(snapshot.document_collections),
        },
        "notes": [
            "Uploaded tabular files are included exactly as uploaded.",
            "Indexed PDF files are not retained by the application, so only their names and counts"
            " are listed in documents/collections.json.",
            "Result tables are limited to the most recent messages the application retains.",
            "Cells that spreadsheet software could run as formulas are neutralised in CSV files.",
        ],
        "files": sorted(files),
    }
    add("manifest.json", _json(manifest))

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, files[path])
    return buffer.getvalue(), len(files)
