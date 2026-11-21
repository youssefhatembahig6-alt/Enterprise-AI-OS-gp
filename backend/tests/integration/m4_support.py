"""Shared M4 harness: a disposable database, the real API with inline dispatch, real
Qdrant, the fake model and the in-memory Drive double (``drive_support``)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from tests.integration.conftest import (
    DbHarness,
    disposable_database,
    document_settings,
    login,
    owner_exec,
)
from tests.integration.document_support import auth
from tests.integration.drive_support import FakeDrive, FakeDriveFactory

ALLOWED_ROOTS = [f"rootFolder{i:03d}" for i in range(1, 60)]
FINANCE_GROUP_EMAIL = "finance-team@drive.example"
TRAVEL = (
    "# Travel policy\n\n## Allowance\n\nThe daily meal allowance for international travel is "
    "60 USD per day. Hotel bookings above 150 USD per night need finance approval.\n"
)
TRAVEL_Q = "What is the daily meal allowance for international travel?"


@dataclass
class M4:
    client: TestClient
    db: DbHarness
    drives: FakeDriveFactory
    settings: Settings
    _roots: Iterator[str]
    config_path: Path

    def set_allowed_roots(self, roots: list[str]) -> None:
        """Rewrite the operator file (the application re-reads it on every use)."""

        raw = json.loads(self.config_path.read_text())
        raw["tenants"]["niletech"]["allowed_roots"] = roots
        self.config_path.write_text(json.dumps(raw))

    def token(self, ref: str) -> str:
        return login(self.client, self.db, ref)

    def new_drive(self, slug: str = "niletech") -> FakeDrive:
        """A fresh Drive folder. Sources from earlier tests are retired (status
        'deleted', which denies their content) so answers come from this test only."""

        owner_exec(
            self.db, "UPDATE connector_sources SET status = 'deleted' WHERE status <> 'deleted'"
        )
        drive = FakeDrive(next(self._roots))
        self.drives.drives[slug] = drive
        return drive

    def user_email(self, ref: str) -> str:
        slug, key = ref.split("/", 1)
        return self.db.foundation.tenants[slug].users[key].email

    def create_source(
        self, token: str, root: str, *, department: str = "finance", **kw: Any
    ) -> dict[str, Any]:
        body = {
            "name": kw.pop("name", f"Drive {root}"),
            "external_root": root,
            "department": department,
            "classification": kw.pop("classification", "internal"),
            "department_restricted": kw.pop("department_restricted", False),
            "allowed_countries": kw.pop("allowed_countries", ["*"]),
        }
        response = self.client.post("/api/v1/admin/sources", json=body, headers=auth(token))
        assert response.status_code == 201, response.text
        return dict(response.json())

    def sync_direct(self, source_id: str, run_id: uuid.UUID | None = None) -> Any:
        """Run one sync in the test thread (no HTTP), for deterministic race tests.
        Passing the same ``run_id`` twice simulates a Celery redelivery."""

        tenant = self.db.foundation.tenants["niletech"].id
        return self.client.app.state.drive_sync.sync(  # type: ignore[attr-defined]
            tenant, uuid.UUID(source_id), trigger="manual", run_id=run_id
        )

    def source_row(self, source_id: str) -> Any:
        return owner_exec(
            self.db,
            "SELECT permissions_current, last_sync_status, last_error_code, claim_run_id,"
            " sync_claimed_until, last_verified_sync_at, policy_version"
            " FROM connector_sources WHERE id = :s",
            {"s": source_id},
        ).one()

    def run_row(self, run_id: Any) -> Any:
        return owner_exec(
            self.db,
            "SELECT status, error_code, started_at, attempt, stats FROM connector_sync_runs"
            " WHERE id = :r",
            {"r": run_id},
        ).one()

    def sync(self, token: str, source_id: str) -> dict[str, Any]:
        response = self.client.post(f"/api/v1/admin/sources/{source_id}/sync", headers=auth(token))
        assert response.status_code == 202, response.text
        return dict(response.json())

    def source(self, token: str, source_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/v1/admin/sources/{source_id}", headers=auth(token))
        assert response.status_code == 200, response.text
        return dict(response.json())

    def document_for(self, source_id: str, file_id: str) -> str | None:
        value = owner_exec(
            self.db,
            "SELECT document_id FROM drive_items WHERE source_id = :s AND external_id = :e",
            {"s": source_id, "e": file_id},
        ).scalar_one_or_none()
        return str(value) if value else None

    def wait_ready(self, document_id: str, version: int = 1, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        status = "?"
        while time.monotonic() < deadline:
            with self.db.owner_engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT v.status, d.active_generation, g.version AS active_version"
                        " FROM document_versions v JOIN documents d ON d.id = v.document_id"
                        " LEFT JOIN document_generations g ON g.document_id = d.id"
                        "   AND g.generation = d.active_generation"
                        " WHERE d.id = :d AND v.version = :v"
                    ),
                    {"d": document_id, "v": version},
                ).one()
            status = row.status
            if status == "ready" and row.active_version == version:
                return
            if status in ("failed", "needs_ocr"):
                break
            time.sleep(0.05)
        raise AssertionError(f"document {document_id} v{version} stuck in {status}")

    def epoch(self, slug: str = "niletech") -> int:
        return int(
            owner_exec(
                self.db,
                "SELECT policy_epoch FROM tenants WHERE slug = :s",
                {"s": slug},
            ).scalar_one()
        )


def build_m4(stub_http_url: str, qdrant_url: str, tmp: Path, **overrides: Any) -> Iterator[M4]:
    config = tmp / "drive-connector.json"
    config.write_text(
        json.dumps(
            {
                "tenants": {
                    "niletech": {
                        # Never used: tests inject the in-memory factory below.
                        "auth": {
                            "type": "oauth_refresh_token",
                            "client_id": "unused",
                            "client_secret": "unused",
                            "refresh_token": "unused",
                        },
                        "allowed_roots": ALLOWED_ROOTS,
                    }
                }
            }
        )
    )
    with disposable_database(seed=True) as db:
        prefix = "m4" + uuid.uuid4().hex[:8]
        settings = document_settings(
            db,
            stub_http_url,
            qdrant_url,
            str(tmp / "objects"),
            prefix,
            DRIVE_CONNECTOR_CONFIG=str(config),
            **overrides,
        )
        drives = FakeDriveFactory()
        with TestClient(create_app(settings, drive_clients=drives)) as client:
            harness = M4(client, db, drives, settings, iter(ALLOWED_ROOTS), config)
            admin = harness.token("niletech/access-admin")
            response = client.put(
                "/api/v1/admin/groups/finance-team/links",
                json={"google_groups": [FINANCE_GROUP_EMAIL]},
                headers=auth(admin),
            )
            assert response.status_code == 200, response.text
            yield harness
            client.app.state.documents._qdrant.delete_collection(  # type: ignore[attr-defined]
                client.app.state.report_runner.documents_workflow.collection()  # type: ignore[attr-defined]
            )


@pytest.fixture(scope="module")
def m4(
    stub_http_url: str, qdrant_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[M4]:
    yield from build_m4(stub_http_url, qdrant_url, tmp_path_factory.mktemp("m4"))
