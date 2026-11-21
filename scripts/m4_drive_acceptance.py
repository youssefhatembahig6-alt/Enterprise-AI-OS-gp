#!/usr/bin/env python3
"""M4 live Google Drive acceptance run against the running stack. Standard library only.

    python3 scripts/m4_drive_acceptance.py --folder <folder id> --file-id <test file id> \
        --reader <Google email>

Prerequisites (docs/evidence/m4/README.md, "Live Drive run"):

1. ``DRIVE_CONNECTOR_CONFIG`` binds ``niletech`` to real Drive credentials and lists
   ``--folder`` in ``allowed_roots``.
2. ``--file-id`` is a text document in that folder which answers ``--question`` and is
   shared (Viewer) with the ``--reader`` Google account as a *user* share.
3. ``--reader`` exists as a NileTech user (``python -m app.cli add-drive-reader``).

Every sync request returns the id of the run it created; the runner waits for **that
run** to reach a terminal state (``GET /admin/sources/{id}/runs/{run}``). A previous
success or an unrelated scheduled run never satisfies the wait. Checks, in order:

1. sync 1 succeeded and the source is ``current``;
2. the test file maps to a document that finishes ingestion;
3. the reader's answer is published and cites **that** document;
4. the citation opens (200, integrity verified) — before revocation;
5. (the operator removes the reader's share in Drive)
6. sync 2 succeeded and detected the sharing change (``permissions_changed >= 1``);
7. a new run of the same question and a differently worded question are both refused
   with ``no_sources``;
8. reopening the original citation returns 404;
9. replaying the original run ends with ``access_revoked`` and no content.

Evidence with a timestamp per step is always written to
``var/m4-drive-acceptance-<UTC>.json`` (also on failure). Exit status: 0 PASS, 1 a check
failed, 2 timeout, 3 setup error.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_Q = "What is the daily meal allowance for international travel?"
REWORDED_Q = "How much is the per-day meal allowance when travelling abroad?"
TERMINAL = frozenset({"succeeded", "failed", "skipped", "superseded"})


def now() -> str:
    return datetime.now(UTC).isoformat()


class AcceptanceTimeout(Exception):
    pass


class SetupError(Exception):
    pass


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self, method: str, path: str, token: str | None = None, body: Any = None
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310
                raw, status = response.read().decode(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode(), exc.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def login(self, tenant: str, email: str, password: str) -> str:
        status, body = self.call(
            "POST",
            "/api/v1/auth/login",
            body={"tenant": tenant, "email": email, "password": password},
        )
        if status != 200:
            raise SetupError(f"login failed for {email}: HTTP {status}")
        return str(body["access_token"])


def sse(raw: str) -> list[dict[str, Any]]:
    frames = []
    for block in raw.split("\n\n"):
        event, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event and data:
            frames.append({"event": event, "data": json.loads("\n".join(data))})
    return frames


def ask(api: Any, token: str, question: str) -> dict[str, Any]:
    started = time.monotonic()
    status, raw = api.call(
        "POST",
        "/api/v1/chat",
        token,
        {"question": question, "mode": "documents", "client_request_id": secrets.token_hex(12)},
    )
    frames = sse(raw) if isinstance(raw, str) else []
    done = next((f["data"] for f in frames if f["event"] == "done"), None) or {}
    return {
        "at": now(),
        "question": question,
        "http_status": status,
        "seconds": round(time.monotonic() - started, 2),
        "run_id": done.get("run_id"),
        "events": [f["event"] for f in frames],
        "outcome": done.get("outcome"),
        "abstention": done.get("abstention"),
        "inference": done.get("inference"),
        "citations": [f["data"] for f in frames if f["event"] == "citation"],
        "errors": [f["data"] for f in frames if f["event"] == "error"],
    }


def request_sync(api: Any, token: str, source_id: str) -> str:
    """Ask for a sync and return the id of the run this request created."""

    status, body = api.call("POST", f"/api/v1/admin/sources/{source_id}/sync", token)
    if status != 202 or not isinstance(body, dict) or not body.get("run_id"):
        raise SetupError(f"sync request failed: HTTP {status} {body}")
    return str(body["run_id"])


def wait_for_run(
    api: Any,
    token: str,
    source_id: str,
    run_id: str,
    *,
    timeout: float = 900.0,
    poll: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll exactly ``run_id`` until it is terminal. Never reads the source's latest
    status, so an earlier success or another run cannot answer for this one."""

    deadline = clock() + timeout
    while True:
        status, run = api.call("GET", f"/api/v1/admin/sources/{source_id}/runs/{run_id}", token)
        if status == 200 and run.get("run_id") == run_id and run.get("status") in TERMINAL:
            return dict(run)
        if status not in (200, 404):  # 404 only before a queued row is visible
            raise SetupError(f"reading run {run_id}: HTTP {status}")
        if clock() >= deadline:
            raise AcceptanceTimeout(f"run {run_id} did not finish within {timeout:.0f} s")
        sleep(poll)


def sync_and_wait(api: Any, token: str, source_id: str, **wait: Any) -> dict[str, Any]:
    requested_at = now()
    run_id = request_sync(api, token, source_id)
    run = wait_for_run(api, token, source_id, run_id, **wait)
    _, source = api.call("GET", f"/api/v1/admin/sources/{source_id}", token)
    return {
        "requested_at": requested_at,
        "run_id": run_id,
        "run": run,
        "observed_at": now(),
        "access": source.get("access"),
        "last_verified_sync_at": source.get("last_verified_sync_at"),
        "items": source.get("items", []),
    }


class Runner:
    def __init__(
        self,
        api: Any,
        args: argparse.Namespace,
        *,
        revoke: Callable[[str], None],
        wait: dict[str, Any] | None = None,
    ) -> None:
        self.api = api
        self.args = args
        self.revoke = revoke
        self.wait = wait or {}
        self.record: dict[str, Any] = {
            "started_at": now(),
            "folder": args.folder,
            "file_id": args.file_id,
            "question": args.question,
            "steps": [],
            "checks": {},
        }

    def step(self, name: str, **data: Any) -> None:
        self.record["steps"].append({"at": now(), "step": name, **data})

    def check(self, name: str, passed: bool) -> bool:
        self.record["checks"][name] = bool(passed)
        print(f"  {'ok ' if passed else 'FAIL'} {name}")
        return bool(passed)

    def run(self, admin: str, reader: str) -> int:  # noqa: PLR0915 - linear acceptance steps
        api, args, rec = self.api, self.args, self.record
        _, rec["tenant_settings"] = api.call("GET", "/api/v1/tenant/settings", admin)
        _, sources = api.call("GET", "/api/v1/admin/sources", admin)
        source = next((s for s in sources if s["external_root"] == args.folder), None)
        if source is None:
            status, source = api.call(
                "POST",
                "/api/v1/admin/sources",
                admin,
                {
                    "name": "M4 live Drive test folder",
                    "external_root": args.folder,
                    "department": "finance",
                    "classification": "internal",
                    "allowed_countries": ["*"],
                },
            )
            if status != 201:
                raise SetupError(f"source creation failed: HTTP {status} {source}")
        source_id = rec["source_id"] = source["source_id"]

        rec["sync_1"] = first = sync_and_wait(api, admin, source_id, **self.wait)
        self.step("sync_1", run_id=first["run_id"], status=first["run"]["status"])
        if not self.check(
            "sync_1_succeeded_and_current",
            first["run"]["status"] == "succeeded" and first["access"] == "current",
        ):
            return 1
        item = next((i for i in first["items"] if i["external_id"] == args.file_id), None)
        document_id = item["document_id"] if item else None
        if not self.check("test_file_imported", bool(document_id) and item["state"] == "active"):
            return 1
        rec["document_id"] = document_id
        clock, sleep = self.wait.get("clock", time.monotonic), self.wait.get("sleep", time.sleep)
        deadline = clock() + float(self.wait.get("timeout", 900.0))
        while True:
            _, docs = api.call("GET", "/api/v1/admin/documents", admin)
            state = next((d["status"] for d in docs if d["document_id"] == document_id), None)
            if state in ("ready", "failed", "needs_ocr"):
                break
            if clock() >= deadline:
                raise AcceptanceTimeout("ingestion did not finish")
            sleep(float(self.wait.get("poll", 2.0)))
        self.step("ingested", document_id=document_id, status=state)
        if not self.check("test_document_ready", state == "ready"):
            return 1

        rec["answer_before"] = before = ask(api, reader, args.question)
        self.step("answer_before", run_id=before["run_id"], outcome=before["outcome"])
        cited = [c for c in before["citations"] if c.get("document_id") == document_id]
        if not self.check(
            "authorized_answer_cites_test_document", before["outcome"] == "published" and cited
        ):
            return 1
        citation = cited[0]
        path = f"/api/v1/runs/{before['run_id']}/citations/{citation['citation_id']}"
        status, opened = api.call("GET", path, reader)
        rec["citation_before"] = {"at": now(), "http_status": status, "body": opened}
        if not self.check(
            "citation_opens_before_revocation",
            status == 200 and isinstance(opened, dict) and opened.get("integrity") == "verified",
        ):
            return 1

        self.revoke(args.reader)
        rec["revoked_in_drive_at"] = now()
        self.step("revoked_in_drive")
        rec["sync_2"] = second = sync_and_wait(api, admin, source_id, **self.wait)
        self.step("sync_2", run_id=second["run_id"], status=second["run"]["status"])
        stats = second["run"].get("stats") or {}
        ok = self.check(
            "sync_2_detected_sharing_change",
            second["run"]["status"] == "succeeded" and stats.get("permissions_changed", 0) >= 1,
        )
        rec["answer_after"] = after = ask(api, reader, args.question)
        rec["answer_reworded"] = reworded = ask(api, reader, REWORDED_Q)
        self.step("answers_after", outcomes=[after["outcome"], reworded["outcome"]])
        ok &= self.check(
            "denied_on_new_questions",
            all(
                (a["abstention"] or {}).get("code") == "no_sources"
                and a["run_id"] != before["run_id"]
                and not a["citations"]
                for a in (after, reworded)
            ),
        )
        status, _ = api.call("GET", path, reader)
        rec["citation_after"] = {"at": now(), "http_status": status}
        ok &= self.check("citation_reopen_denied", status == 404)
        status, raw = api.call("GET", f"/api/v1/runs/{before['run_id']}/events?after=0", reader)
        frames = sse(raw) if isinstance(raw, str) else []
        rec["replay_after"] = {
            "at": now(),
            "http_status": status,
            "events": [f["event"] for f in frames],
            "errors": [f["data"] for f in frames if f["event"] == "error"],
        }
        ok &= self.check(
            "original_run_replay_revoked",
            any(e.get("code") == "access_revoked" for e in rec["replay_after"]["errors"])
            and not any(f["event"] in ("token", "citation") for f in frames),
        )
        return 0 if ok else 1


def write_evidence(record: dict[str, Any], out_dir: Path) -> Path:
    record["finished_at"] = now()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"m4-drive-acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True))
    return out


def prompt_revoke(reader: str) -> None:
    input(f"\nRemove {reader} from the test file's sharing in Drive now, then press Enter… ")


def main(
    argv: list[str] | None = None,
    *,
    api: Any = None,
    revoke: Callable[[str], None] = prompt_revoke,
    wait: dict[str, Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--tenant", default="niletech")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--file-id", required=True, help="Drive id of the shared test file")
    parser.add_argument("--reader", required=True, help="Google account the file is shared with")
    parser.add_argument("--admin", default="access.admin@niletech.example")
    parser.add_argument("--question", default=DEFAULT_Q)
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    parser.add_argument("--out-dir", default=str(ROOT / "var"))
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    client = api or Api(args.api)
    runner = Runner(
        client, args, revoke=revoke, wait=wait or {"timeout": args.timeout, "poll": 2.0}
    )
    code = 3
    try:
        creds = json.loads(Path(args.credentials).read_text())[args.tenant]
        admin = client.login(args.tenant, args.admin, creds[args.admin])
        reader = client.login(args.tenant, args.reader.lower(), creds[args.reader.lower()])
        code = runner.run(admin, reader)
    except AcceptanceTimeout as exc:
        code, runner.record["error"] = 2, f"timeout: {exc}"
    except (SetupError, KeyError, OSError) as exc:
        code, runner.record["error"] = 3, f"setup: {exc}"
    runner.record["result"] = {0: "PASS", 1: "FAIL", 2: "TIMEOUT", 3: "SETUP_ERROR"}[code]
    runner.record["exit_code"] = code
    out = write_evidence(runner.record, Path(args.out_dir))
    print(f"\n{runner.record['result']}\nwrote {out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
