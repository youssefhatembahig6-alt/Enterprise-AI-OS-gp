"""The live Drive acceptance runner (scripts/m4_drive_acceptance.py) against a scripted
fake API: a delayed worker, an earlier success and an unrelated scheduled run must never
satisfy the wait, and every failure or timeout exits nonzero with evidence written."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "m4_drive_acceptance.py"
_spec = importlib.util.spec_from_file_location("m4_drive_acceptance", SCRIPT)
assert _spec and _spec.loader
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

SOURCE, FILE, DOC = "src-1", "file-travel", "doc-travel"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def sse(*frames: tuple[str, dict[str, Any]]) -> str:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in frames)


class FakeApi:
    """Every sync request gets a new run that stays queued/running for ``delay`` polls.
    Meanwhile the source view always reports an *earlier* success, and an unrelated
    scheduled run is visible too; neither may end the wait."""

    def __init__(self, delay: int = 3, *, cite: str = DOC, never_finish: bool = False) -> None:
        self.delay, self.cite, self.never_finish = delay, cite, never_finish
        self.runs: dict[str, list[str]] = {"run-scheduled": ["succeeded"]}
        self.revoked = False
        self.calls: list[tuple[str, str]] = []

    def login(self, tenant: str, email: str, password: str) -> str:
        return f"token-{email}"

    def call(self, method: str, path: str, token: str | None = None, body: Any = None) -> Any:
        self.calls.append((method, path))
        if path == "/api/v1/tenant/settings":
            return 200, {"enabled_connectors": ["google_drive"]}
        if path == "/api/v1/admin/sources" and method == "GET":
            return 200, [{"source_id": SOURCE, "external_root": "folder-1"}]
        if path.endswith("/sync"):
            run_id = f"run-{len(self.runs)}"
            states = ["queued"] * self.delay + ["running"] * self.delay
            self.runs[run_id] = states if self.never_finish else [*states, "succeeded"]
            return 202, {"status": "queued", "run_id": run_id}
        if "/runs/" in path and path.startswith("/api/v1/admin/sources/"):
            run_id = path.rsplit("/", 1)[1]
            states = self.runs[run_id]
            status = states.pop(0) if len(states) > 1 else states[0]
            stats = {"permissions_changed": 1} if self.revoked else {"created": 1}
            return 200, {"run_id": run_id, "status": status, "stats": stats}
        if path == f"/api/v1/admin/sources/{SOURCE}":
            return 200, {
                "access": "current",
                "last_sync_status": "succeeded",  # an earlier run's success
                "last_verified_sync_at": "2026-11-21T00:00:00+00:00",
                "items": [{"external_id": FILE, "document_id": DOC, "state": "active"}],
            }
        if path == "/api/v1/admin/documents":
            return 200, [{"document_id": DOC, "status": "ready"}]
        if path == "/api/v1/chat":
            if self.revoked:
                return 200, sse(
                    (
                        "done",
                        {
                            "run_id": f"r-{len(self.calls)}",
                            "outcome": "abstained",
                            "abstention": {"code": "no_sources"},
                        },
                    ),
                )
            return 200, sse(
                ("citation", {"citation_id": "c1", "document_id": self.cite}),
                ("done", {"run_id": "answer-1", "outcome": "published", "inference": "fake"}),
            )
        if "/citations/" in path:
            return (404, {}) if self.revoked else (200, {"integrity": "verified"})
        if path.endswith("/events?after=0"):
            return 200, sse(("error", {"code": "access_revoked"}), ("done", {}))
        raise AssertionError(f"unexpected call {method} {path}")


def run_main(api: FakeApi, tmp_path: Path, clock: Clock, timeout: float = 600) -> int:
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {"niletech": {"access.admin@niletech.example": "x", "reader@gmail.example": "y"}}
        )
    )

    def revoke(_: str) -> None:
        api.revoked = True

    return int(
        runner.main(
            [
                "--folder",
                "folder-1",
                "--file-id",
                FILE,
                "--reader",
                "reader@gmail.example",
                "--credentials",
                str(creds),
                "--out-dir",
                str(tmp_path / "out"),
            ],
            api=api,
            revoke=revoke,
            wait={"timeout": timeout, "poll": 2.0, "clock": clock, "sleep": clock.sleep},
        )
    )


def evidence(tmp_path: Path) -> dict[str, Any]:
    (path,) = (tmp_path / "out").glob("m4-drive-acceptance-*.json")
    return dict(json.loads(path.read_text()))


def test_wait_returns_only_when_the_requested_run_finishes() -> None:
    api, clock = FakeApi(delay=3), Clock()
    run_id = runner.request_sync(api, "t", SOURCE)
    run = runner.wait_for_run(api, "t", SOURCE, run_id, clock=clock, sleep=clock.sleep)
    assert run["run_id"] == run_id and run["status"] == "succeeded"
    polls = [c for c in api.calls if c[1].endswith(f"/runs/{run_id}")]
    assert len(polls) == 7  # 3 queued + 3 running + the terminal read
    # Neither the source's earlier success nor the scheduled run was consulted.
    assert ("GET", f"/api/v1/admin/sources/{SOURCE}") not in api.calls
    assert not any(c[1].endswith("/runs/run-scheduled") for c in api.calls)


def test_wait_times_out_on_a_run_that_never_finishes() -> None:
    api, clock = FakeApi(never_finish=True), Clock()
    run_id = runner.request_sync(api, "t", SOURCE)
    with pytest.raises(runner.AcceptanceTimeout):
        runner.wait_for_run(api, "t", SOURCE, run_id, timeout=30, clock=clock, sleep=clock.sleep)


def test_full_sequence_passes_with_a_delayed_worker(tmp_path: Path) -> None:
    api = FakeApi(delay=4)
    assert run_main(api, tmp_path, Clock()) == 0
    record = evidence(tmp_path)
    assert record["result"] == "PASS" and all(record["checks"].values())
    assert record["sync_1"]["run_id"] != record["sync_2"]["run_id"]
    assert [s["step"] for s in record["steps"]] == [
        "sync_1",
        "ingested",
        "answer_before",
        "revoked_in_drive",
        "sync_2",
        "answers_after",
    ]
    assert all(s["at"] for s in record["steps"])


def test_timeout_exits_nonzero_and_still_writes_evidence(tmp_path: Path) -> None:
    assert run_main(FakeApi(never_finish=True), tmp_path, Clock(), timeout=60) == 2
    record = evidence(tmp_path)
    assert record["result"] == "TIMEOUT" and record["exit_code"] == 2


def test_answer_citing_another_document_fails(tmp_path: Path) -> None:
    assert run_main(FakeApi(cite="doc-other"), tmp_path, Clock()) == 1
    record = evidence(tmp_path)
    assert record["checks"]["authorized_answer_cites_test_document"] is False
    assert record["result"] == "FAIL"
