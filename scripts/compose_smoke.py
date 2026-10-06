"""End-to-end smoke test for the local container stack (standard library only).

Drives the same HTTP surface a browser uses (through the web proxy) and, with
``--compose``, also checks hardening and restart behavior via ``docker compose``::

    LOCAL_ONLY_MODE=true docker compose up -d --wait
    python -m scripts.compose_smoke --compose --expect-local-only

Without ``--compose`` it only needs a reachable API or web URL, which makes the
request flow testable against a plain ``uvicorn`` process.

Journey: readiness, workspace, CSV upload, profile, schema confirmation,
deterministic analysis, chat (session-memory route; deterministic, so it needs
no model provider), Markdown/PDF report download, deletion, then (compose only)
non-root/read-only checks, an API restart, orphan recovery, and audit-chain
persistence. PDF/RAG indexing is not exercised because it needs the embedding
model download; that path is covered by the API test suite and the evaluations.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = REPO_ROOT / "sample_data" / "transactions.csv"
MEMORY_QUESTION = "What limitations or unavailable analysis apply here?"


class SmokeFailure(AssertionError):
    """A smoke check failed; the message says which one."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"  ok  {message}")


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, _lowercase(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, _lowercase(error.headers), error.read()

    def json(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        expect: int = 200,
    ) -> tuple[Any, dict[str, str]]:
        merged = {"Content-Type": "application/json", **(headers or {})}
        data = json.dumps(payload).encode() if payload is not None else None
        status, response_headers, raw = self.request(method, path, body=data, headers=merged)
        if status != expect:
            raise SmokeFailure(f"{method} {path} returned {status}, expected {expect}")
        return (json.loads(raw) if raw else None), response_headers

    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status, _, _ = self.request("GET", "/health/ready")
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(2)
        raise SmokeFailure(f"The service was not ready within {timeout:g} seconds.")


def _lowercase(headers: Any) -> dict[str, str]:
    """Header names are case-insensitive; servers differ in how they send them."""
    return {key.lower(): value for key, value in headers.items()}


def multipart_file(
    field: str, filename: str, content: bytes, content_type: str
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
    ).encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), boundary


def compose(*args: str, check_exit: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if check_exit and result.returncode != 0:
        raise SmokeFailure(f"docker compose {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def audit_state() -> dict[str, Any]:
    output = compose("exec", "-T", "api", "python", "-m", "scripts.verify_audit", "--json")
    state: dict[str, Any] = json.loads(output.stdout)
    return state


def confirmed_dataset(client: Client, workspace_id: str, csv_path: Path) -> dict[str, Any]:
    """Upload, profile, and confirm a dataset (confirmation writes workspace storage)."""
    body, boundary = multipart_file("file", csv_path.name, csv_path.read_bytes(), "text/csv")
    status, _, raw = client.request(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/tabular-uploads",
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    check(status == 201, "CSV upload accepted")
    upload_id = json.loads(raw)["id"]

    dataset, _ = client.json("POST", f"/api/v1/tabular-uploads/{upload_id}/dataset", {}, expect=201)
    check(dataset["row_count"] > 0 and dataset["status"] == "review_required", "profile created")
    mapping = {
        field: value["source_column"]
        for field, value in dataset["schema_mapping"]["mappings"].items()
    }
    confirmed, _ = client.json("PUT", f"/api/v1/datasets/{dataset['id']}/schema-mapping", mapping)
    check(confirmed["status"] == "ready", "schema mapping confirmed")
    result: dict[str, Any] = confirmed
    return result


def run_journey(client: Client, csv_path: Path, expect_local_only: bool) -> None:
    print("Workspace and tabular journey")
    workspace, _ = client.json(
        "POST",
        "/api/v1/workspaces",
        headers={"Idempotency-Key": uuid.uuid4().hex},
        expect=201,
    )
    workspace_id = workspace["id"]
    if expect_local_only:
        check(workspace["local_only_mode"] is True, "workspace reports local-only mode")
        check(not workspace["consent_required"], "no model consent is required in local-only mode")

    dataset = confirmed_dataset(client, workspace_id, csv_path)

    analysis, _ = client.json("POST", f"/api/v1/datasets/{dataset['id']}/analyses", {}, expect=201)
    check(bool(analysis["charts"]) and bool(analysis["report"]["sections"]), "analysis produced")

    conversation, _ = client.json(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/conversations",
        {"dataset_id": dataset["id"]},
        expect=201,
    )
    message, headers = client.json(
        "POST",
        f"/api/v1/conversations/{conversation['id']}/messages",
        {"question": MEMORY_QUESTION},
        expect=201,
    )
    check(message["route"] == "memory", "chat answered by the deterministic memory route")
    check(message["request_id"] == headers.get("x-request-id"), "message carries the request ID")
    check(message["provenance"]["grounding_status"] == "not_applicable", "provenance present")

    report, _ = client.json(
        "POST", f"/api/v1/analyses/{analysis['id']}/reports", {"include_charts": False}, expect=201
    )
    status, _, markdown = client.request(
        "GET", f"/api/v1/reports/{report['id']}/content?format=markdown"
    )
    check(status == 200 and b"Business Intelligence" in markdown, "Markdown report downloads")
    status, _, pdf = client.request("GET", f"/api/v1/reports/{report['id']}/content?format=pdf")
    check(status == 200 and pdf.startswith(b"%PDF"), "PDF report downloads")

    status, _, _ = client.request("DELETE", f"/api/v1/workspaces/{workspace_id}")
    check(status == 204, "workspace deleted")
    status, _, raw = client.request("GET", f"/api/v1/workspaces/{workspace_id}")
    check(status == 404, "deleted workspace is gone")
    check("request_id" in json.loads(raw)["error"], "errors carry a request ID")


def run_compose_checks(client: Client, timeout: float, csv_path: Path) -> None:
    print("Container hardening")
    for service in ("api", "web"):
        uid = compose("exec", "-T", service, "id", "-u").stdout.strip()
        check(uid.isdigit() and uid != "0", f"{service} runs as non-root (uid {uid})")
    write = compose("exec", "-T", "api", "sh", "-c", "touch /probe", check_exit=False)
    check(write.returncode != 0, "api root filesystem is read-only")

    status, headers, _ = client.request("GET", "/")
    check(status == 200, "web serves the application shell")
    check(headers.get("x-content-type-options") == "nosniff", "web sends nosniff")
    check("frame-ancestors" in headers.get("content-security-policy", ""), "web sends a CSP")
    _, _, docs = client.request("GET", "/docs")
    check(b"swagger" not in docs.lower(), "interactive API docs are not exposed by the web proxy")

    print("Restart, recovery, and audit persistence")
    survivor, _ = client.json(
        "POST", "/api/v1/workspaces", headers={"Idempotency-Key": uuid.uuid4().hex}, expect=201
    )
    survivor_dataset = confirmed_dataset(client, survivor["id"], csv_path)
    find_workspaces = "find /data/api -mindepth 2 -maxdepth 2 -name 'ws_*'"
    on_disk = compose("exec", "-T", "api", "sh", "-c", find_workspaces).stdout.split()
    check(len(on_disk) == 1, "the live workspace has files on the data volume")
    # A directory no metadata row owns, as left by a crash or a lost database.
    orphan = "/data/api/" + "c" * 32 + "/ws_" + "d" * 32
    compose("exec", "-T", "api", "mkdir", "-p", orphan)
    before = audit_state()
    records_before = sum(item["records"] for item in before["tenants"].values())
    check(before["ok"] and records_before > 0, f"audit chain intact ({records_before} record(s))")

    compose("restart", "api")
    client.wait_ready(timeout)
    check(True, "API healthy again after restart")
    # Durable metadata (ADR 0022): the workspace and its uploaded file are recovered.
    restored, _ = client.json("GET", f"/api/v1/workspaces/{survivor['id']}", expect=200)
    check(restored["id"] == survivor["id"], "workspace metadata survived the restart")
    recovered, _ = client.json("GET", f"/api/v1/datasets/{survivor_dataset['id']}", expect=200)
    check(
        recovered["status"] == "ready" and recovered["row_count"] == survivor_dataset["row_count"],
        "confirmed dataset rebuilt from the recovered upload",
    )
    client.json(
        "POST", f"/api/v1/tabular-uploads/{survivor_dataset['upload_id']}/dataset", {}, expect=201
    )
    check(True, "recovered upload can seed a new dataset")
    leftovers = compose("exec", "-T", "api", "sh", "-c", find_workspaces).stdout.split()
    check(
        len(leftovers) == 1 and leftovers[0].endswith(survivor["id"]),
        "only the recovered workspace remains; the orphaned directory was swept at startup",
    )
    after = audit_state()
    records_after = sum(item["records"] for item in after["tenants"].values())
    check(after["ok"] and records_after > records_before, "audit chain survived and continues")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--expect-local-only", action="store_true")
    parser.add_argument("--compose", action="store_true", help="Also run docker compose checks.")
    args = parser.parse_args(argv)

    if not args.csv.is_file():
        print(f"CSV file not found: {args.csv}", file=sys.stderr)
        return 2

    client = Client(args.base_url)
    try:
        print(f"Waiting for {args.base_url} ...")
        client.wait_ready(args.timeout)
        run_journey(client, args.csv, args.expect_local_only)
        if args.compose:
            run_compose_checks(client, args.timeout, args.csv)
    except SmokeFailure as failure:
        print(f"SMOKE TEST FAILED: {failure}", file=sys.stderr)
        return 1
    print("Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
