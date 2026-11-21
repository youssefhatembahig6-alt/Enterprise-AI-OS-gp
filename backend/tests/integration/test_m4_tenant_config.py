"""M4 tenant configuration (FR-12): two tenants with different packs, connectors and
sync schedules from fixtures/data only, and backend enforcement when a user requests a
disabled capability directly (API) or the capability is switched off mid-run (worker)."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.conftest import owner_exec
from tests.integration.document_support import auth
from tests.integration.m4_support import M4, m4
from tests.integration.report_support import ask

__all__ = ["m4"]

ADMIN = "niletech/access-admin"
ISO_READER = "isolation-test/finance-reader"
SALES_Q = "What were fulfilled sales in Q1 2026?"


def settings_of(m4: M4, ref: str) -> dict[str, Any]:
    response = m4.client.get("/api/v1/tenant/settings", headers=auth(m4.token(ref)))
    assert response.status_code == 200
    return dict(response.json())


def put_settings(m4: M4, body: dict[str, Any], ref: str = ADMIN) -> Any:
    return m4.client.put("/api/v1/admin/tenant/settings", json=body, headers=auth(m4.token(ref)))


@pytest.fixture
def restore_niletech(m4: M4) -> Iterator[None]:
    yield
    response = put_settings(
        m4,
        {
            "enabled_packs": ["documents", "sales_reports"],
            "enabled_connectors": ["upload", "google_drive"],
            "sync_mode": "demo",
        },
    )
    assert response.status_code == 200


def runs_of(m4: M4, ref: str) -> int:
    slug, key = ref.split("/", 1)
    user = m4.db.foundation.tenants[slug].users[key].id
    return int(
        owner_exec(
            m4.db, "SELECT count(*) FROM report_runs WHERE owner_user_id = :u", {"u": user}
        ).scalar_one()
    )


def test_two_tenants_have_different_configuration_from_fixtures(m4: M4) -> None:
    nile = settings_of(m4, "niletech/finance-reader")
    iso = settings_of(m4, ISO_READER)
    assert nile["enabled_packs"] == ["documents", "sales_reports"]
    assert nile["enabled_connectors"] == ["google_drive", "upload"]
    assert (nile["sync_mode"], nile["sync_interval_seconds"]) == ("demo", 900)
    assert iso["enabled_packs"] == ["documents"]
    assert iso["enabled_connectors"] == ["upload"]
    assert (iso["sync_mode"], iso["sync_interval_seconds"]) == ("nightly", 86400)


def test_direct_request_for_a_disabled_pack_is_refused(m4: M4) -> None:
    before = runs_of(m4, ISO_READER)
    response = m4.client.post(
        "/api/v1/chat",
        json={"question": SALES_Q, "mode": "report"},
        headers=auth(m4.token(ISO_READER)),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"
    assert runs_of(m4, ISO_READER) == before  # nothing was created or queued


def test_auto_routing_only_uses_enabled_packs(m4: M4) -> None:
    stream = ask(m4.client, m4.token(ISO_READER), SALES_Q)
    assert stream.status_code == 200
    kind = owner_exec(
        m4.db, "SELECT kind FROM report_runs WHERE id = :r", {"r": stream.run_id}
    ).scalar_one()
    assert kind == "documents"  # the sales tool is never invoked for this tenant


def test_disabled_upload_connector_is_refused(m4: M4, restore_niletech: None) -> None:
    assert put_settings(m4, {"enabled_connectors": ["google_drive"]}).status_code == 200
    body = {
        "filename": "note.md",
        "content_base64": base64.b64encode(b"# Note\n\nText.\n").decode(),
        "department": "finance",
    }
    response = m4.client.post("/api/v1/documents", json=body, headers=auth(m4.token(ADMIN)))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"
    iso = m4.client.post(
        "/api/v1/documents", json=body, headers=auth(m4.token("isolation-test/finance-admin"))
    )
    assert iso.status_code == 202  # the other tenant's configuration is untouched


def test_pack_disabled_during_a_run_publishes_nothing(m4: M4, restore_niletech: None) -> None:
    body = {
        "filename": "pack-check.md",
        "content_base64": base64.b64encode(
            b"# Pack check\n\nThe pack check marker phrase is cobalt heron.\n"
        ).decode(),
        "department": "finance",
        "permissions": {
            "classification": "internal",
            "grants": [{"effect": "allow", "group": "finance-team"}],
        },
    }
    uploaded = m4.client.post("/api/v1/documents", json=body, headers=auth(m4.token(ADMIN)))
    m4.wait_ready(uploaded.json()["document_id"])
    workflow = m4.client.app.state.report_runner.documents_workflow  # type: ignore[attr-defined]
    admin = m4.token(ADMIN)

    def before(name: str, _: Any) -> None:
        if name == "publish":
            response = m4.client.put(
                "/api/v1/admin/tenant/settings",
                json={"enabled_packs": ["sales_reports"]},
                headers=auth(admin),
            )
            assert response.status_code == 200

    workflow.deps.before_node = before
    try:
        stream = ask(
            m4.client,
            m4.token("niletech/finance-reader"),
            "What is the pack check marker phrase?",
            mode="documents",
        )
    finally:
        workflow.deps.before_node = None
    assert stream.done["outcome"] == "failed", stream.raw[-400:]
    assert {f.data["code"] for f in stream.of("error")} == {"capability_disabled"}
    assert not stream.of("token") and not stream.of("citation")
    assert "cobalt heron" not in stream.raw


def test_worker_refuses_a_run_whose_pack_was_disabled_before_it_started(
    m4: M4, restore_niletech: None
) -> None:
    """A run row that reaches the worker after the pack was switched off (for example a
    queued job) is refused at the start, before any retrieval or model call."""

    report = m4.client.app.state.report_runner.workflow  # type: ignore[attr-defined]

    def before(name: str, _: Any) -> None:
        if name == "prepare":
            owner_exec(
                m4.db,
                "UPDATE tenant_settings SET enabled_packs = '{documents}' WHERE tenant_id = :t",
                {"t": m4.db.foundation.tenants["niletech"].id},
            )

    report.deps.before_node = before
    try:
        stream = ask(m4.client, m4.token("niletech/finance-reader"), SALES_Q, mode="report")
    finally:
        report.deps.before_node = None
    assert stream.done["outcome"] == "failed"
    assert {f.data["code"] for f in stream.of("error")} == {"capability_disabled"}
    steps = [f.data.get("step") for f in stream.of("status")]
    assert "authorization" not in steps  # stopped before the dataset was even authorized


def test_tenant_configuration_needs_tenant_wide_tenant_config(m4: M4) -> None:
    dept = put_settings(m4, {"enabled_packs": ["documents"]}, ref="niletech/finance-admin")
    assert dept.status_code == 403
    iso = put_settings(m4, {"enabled_packs": ["documents"]}, ref="isolation-test/finance-admin")
    assert iso.status_code == 403
    bogus = put_settings(m4, {"enabled_packs": ["documents", "legal"]})
    assert bogus.status_code == 422  # M5 pack is not implemented yet
    assert settings_of(m4, ADMIN)["enabled_packs"] == ["documents", "sales_reports"]


def test_configuration_changes_are_versioned_and_audited(m4: M4, restore_niletech: None) -> None:
    version = settings_of(m4, ADMIN)["version"]
    changed = put_settings(m4, {"sync_mode": "nightly"})
    assert changed.status_code == 200
    assert changed.json()["version"] == version + 1
    assert changed.json()["sync_interval_seconds"] == 86400
    events = m4.client.get(
        "/api/v1/admin/audit",
        params={"event_type": "admin.tenant_settings"},
        headers=auth(m4.token(ADMIN)),
    ).json()["events"]
    assert events[0]["details"]["sync_mode"] == "nightly"
