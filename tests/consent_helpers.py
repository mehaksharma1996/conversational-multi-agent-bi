"""Accept a workspace's consent the way the web client does: the served notice, with its digest.

A digest-free ``2026-09`` acceptance records only the legacy Gemini recipient, so a harness whose
configuration names any other recipient (for example the scripted investigation provider) must
accept the notice it was served.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def accept_current_notice(client: TestClient, workspace_id: str) -> Any:
    url = f"/api/v1/workspaces/{workspace_id}/consent"
    notice = client.get(url).json()["notice"]
    return client.put(
        url,
        json={
            "accepted": True,
            "notice_version": notice["version"],
            "notice_digest": notice["digest"],
        },
    )
