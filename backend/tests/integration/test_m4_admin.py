"""M4 scoped administration on real PostgreSQL: users, roles, groups, documents, audit,
department vs tenant scope, and removal of access through the admin API taking effect
on the next request, before an active answer is published, and on reopen/replay."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.conftest import owner_exec
from tests.integration.document_support import auth
from tests.integration.m4_support import M4, m4
from tests.integration.report_support import ask, replay

__all__ = ["m4"]

FINANCE = "niletech/finance-reader"
ADMIN = "niletech/access-admin"
DEPT_ADMIN = "niletech/finance-admin"
POLICY = (
    "# Expense policy\n\n## Receipts\n\nEvery expense claim above 25 USD needs an itemised "
    "receipt attached within 30 days.\n"
)
POLICY_Q = "Which expense claims need an itemised receipt?"


@pytest.fixture(scope="module")
def expense_doc(m4: M4) -> str:
    body = {
        "filename": "expense-policy.md",
        "content_base64": base64.b64encode(POLICY.encode()).decode(),
        "department": "finance",
        "title": "Expense policy",
        "permissions": {
            "classification": "internal",
            "department_restricted": False,
            "allowed_countries": ["*"],
            "grants": [{"effect": "allow", "group": "finance-team"}],
        },
    }
    response = m4.client.post("/api/v1/documents", json=body, headers=auth(m4.token(ADMIN)))
    assert response.status_code == 202, response.text
    doc = str(response.json()["document_id"])
    m4.wait_ready(doc)
    return doc


def uid(m4: M4, ref: str) -> str:
    slug, key = ref.split("/", 1)
    return str(m4.db.foundation.tenants[slug].users[key].id)


def get(m4: M4, token: str, path: str) -> Any:
    return m4.client.get(f"/api/v1{path}", headers=auth(token))


@pytest.fixture
def restore_finance_reader(m4: M4) -> Iterator[None]:
    yield
    admin = m4.token(ADMIN)
    m4.client.patch(
        f"/api/v1/admin/users/{uid(m4, FINANCE)}",
        json={"status": "active", "clearance": "confidential"},
        headers=auth(admin),
    )
    m4.client.put(
        f"/api/v1/admin/users/{uid(m4, FINANCE)}/groups",
        json={"groups": ["all-staff", "finance-team"]},
        headers=auth(admin),
    )
    m4.client.put(
        f"/api/v1/admin/users/{uid(m4, FINANCE)}/roles",
        json={"assignments": [{"role": "reader"}, {"role": "report_reader"}]},
        headers=auth(admin),
    )


# -- users and scope ---------------------------------------------------------------------


def test_user_listing_follows_identity_admin_scope(m4: M4) -> None:
    everyone = {u["email"] for u in get(m4, m4.token(ADMIN), "/admin/users").json()}
    finance = get(m4, m4.token(DEPT_ADMIN), "/admin/users").json()
    assert {u["department"] for u in finance} == {"finance"}
    assert m4.user_email("niletech/sales-reader") in everyone
    assert m4.user_email("niletech/sales-reader") not in {u["email"] for u in finance}
    assert get(m4, m4.token(FINANCE), "/admin/users").status_code == 403
    iso = get(m4, m4.token("isolation-test/finance-admin"), "/admin/users").json()
    assert all(u["email"].endswith("@isolation.example") for u in iso)


def test_department_admin_cannot_reach_other_departments(m4: M4) -> None:
    token = m4.token(DEPT_ADMIN)
    sales = uid(m4, "niletech/sales-reader")
    response = m4.client.patch(
        f"/api/v1/admin/users/{sales}", json={"status": "inactive"}, headers=auth(token)
    )
    assert response.status_code == 404  # outside scope looks like absence
    move = m4.client.patch(
        f"/api/v1/admin/users/{uid(m4, FINANCE)}", json={"department": "hr"}, headers=auth(token)
    )
    assert move.status_code == 403
    groups = m4.client.put(
        f"/api/v1/admin/users/{uid(m4, FINANCE)}/groups",
        json={"groups": ["all-staff"]},
        headers=auth(token),
    )
    assert groups.status_code == 403  # group membership is a tenant-wide operation


@pytest.mark.parametrize(
    ("assignment", "status"),
    [
        ({"role": "access_admin"}, 403),  # tenant-wide admin power the assigner lacks
        ({"role": "department_admin", "scope_department": "hr"}, 403),  # outside scope
        ({"role": "report_reader", "scope_department": "finance"}, 200),  # content, in scope
        ({"role": "department_admin", "scope_department": "finance"}, 200),  # held power
    ],
)
def test_role_assignment_cannot_escalate(m4: M4, assignment: dict[str, Any], status: int) -> None:
    token = m4.token(DEPT_ADMIN)
    target = uid(m4, "niletech/denied-reader")
    current = [{"role": "reader"}, {"role": "report_reader"}]  # tenant-wide, kept unchanged
    response = m4.client.put(
        f"/api/v1/admin/users/{target}/roles",
        json={"assignments": [*current, assignment]},
        headers=auth(token),
    )
    assert response.status_code == status, response.text
    m4.client.put(
        f"/api/v1/admin/users/{target}/roles",
        json={"assignments": current},
        headers=auth(m4.token(ADMIN)),
    )


def test_scoped_admin_cannot_remove_tenant_wide_assignments(m4: M4) -> None:
    target = uid(m4, "niletech/denied-reader")
    response = m4.client.put(
        f"/api/v1/admin/users/{target}/roles",
        json={"assignments": [{"role": "report_reader"}]},
        headers=auth(m4.token(DEPT_ADMIN)),
    )
    assert response.status_code == 403


def test_admins_cannot_lock_themselves_out(m4: M4) -> None:
    token = m4.token(ADMIN)
    me = uid(m4, ADMIN)
    response = m4.client.patch(
        f"/api/v1/admin/users/{me}", json={"status": "inactive"}, headers=auth(token)
    )
    assert response.status_code == 422
    roles = m4.client.put(
        f"/api/v1/admin/users/{me}/roles", json={"assignments": []}, headers=auth(token)
    )
    assert roles.status_code == 422


def test_role_definitions_are_tenant_wide_and_bounded(m4: M4) -> None:
    dept = m4.client.put(
        "/api/v1/admin/roles/auditor",
        json={"name": "Auditor", "capabilities": ["audit.read"]},
        headers=auth(m4.token(DEPT_ADMIN)),
    )
    assert dept.status_code == 403
    ok = m4.client.put(
        "/api/v1/admin/roles/auditor",
        json={"name": "Auditor", "capabilities": ["audit.read"]},
        headers=auth(m4.token(ADMIN)),
    )
    assert ok.status_code == 200
    beyond = m4.client.put(
        "/api/v1/admin/roles/superuser",
        json={"name": "Super", "capabilities": ["dataset.query", "bogus"]},
        headers=auth(m4.token(ADMIN)),
    )
    assert beyond.status_code == 422
    roles = {r["key"]: r for r in get(m4, m4.token(ADMIN), "/admin/roles").json()}
    assert roles["auditor"]["capabilities"] == ["audit.read"]


# -- documents: metadata scope never implies content ---------------------------------------


def test_document_admin_scope_and_no_content_from_metadata_rights(m4: M4, expense_doc: str) -> None:
    finance_docs = get(m4, m4.token(DEPT_ADMIN), "/admin/documents").json()
    assert {d["department"] for d in finance_docs} == {"finance"}
    (row,) = [d for d in finance_docs if d["document_id"] == expense_doc]
    assert row["grants"] == ["allow:group:finance-team"] and row["permissions_current"]
    assert "receipt" not in str(row)  # metadata only, never document text
    # resource.admin over finance does not let the department admin read it.
    stream = ask(m4.client, m4.token(DEPT_ADMIN), POLICY_Q, mode="documents")
    assert stream.done["abstention"]["code"] == "no_sources"


# -- removal of access: next request, active run, reopen/replay -----------------------------


def removals(m4: M4) -> dict[str, Any]:
    target = uid(m4, FINANCE)
    return {
        "deactivate": (f"/api/v1/admin/users/{target}", "patch", {"status": "inactive"}),
        "group": (f"/api/v1/admin/users/{target}/groups", "put", {"groups": ["all-staff"]}),
        "role": (
            f"/api/v1/admin/users/{target}/roles",
            "put",
            {"assignments": [{"role": "report_reader"}]},
        ),
        "clearance": (f"/api/v1/admin/users/{target}", "patch", {"clearance": "public"}),
    }


def remove(m4: M4, kind: str) -> None:
    path, method, body = removals(m4)[kind]
    response = getattr(m4.client, method)(path, json=body, headers=auth(m4.token(ADMIN)))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["deactivate", "group", "role", "clearance"])
def test_removal_applies_to_the_next_request(
    m4: M4, expense_doc: str, kind: str, restore_finance_reader: None
) -> None:
    token = m4.token(FINANCE)
    assert ask(m4.client, token, POLICY_Q, mode="documents").done["outcome"] == "published"
    remove(m4, kind)
    if kind == "deactivate":
        response = m4.client.post(
            "/api/v1/chat", json={"question": POLICY_Q, "mode": "documents"}, headers=auth(token)
        )
        assert response.status_code == 401
        return
    stream = ask(m4.client, token, POLICY_Q, mode="documents")
    assert stream.done["abstention"]["code"] == "no_sources"
    assert "itemised receipt" not in stream.raw
    listing = get(m4, token, "/documents").json()
    assert expense_doc not in {d["document_id"] for d in listing}


@pytest.mark.parametrize("kind", ["group", "role", "clearance"])
@pytest.mark.parametrize("node", ["draft", "verify", "publish"])
def test_removal_during_an_active_run_blocks_publication(
    m4: M4, expense_doc: str, kind: str, node: str, restore_finance_reader: None
) -> None:
    workflow = m4.client.app.state.report_runner.documents_workflow  # type: ignore[attr-defined]
    admin_token = m4.token(ADMIN)
    path, method, body = removals(m4)[kind]

    def before(name: str, _: Any) -> None:
        if name == node:  # the real admin API, called while the run is in flight
            response = getattr(m4.client, method)(path, json=body, headers=auth(admin_token))
            assert response.status_code == 200, response.text

    workflow.deps.before_node = before
    try:
        stream = ask(m4.client, m4.token(FINANCE), POLICY_Q, mode="documents")
    finally:
        workflow.deps.before_node = None
    assert stream.done["outcome"] == "failed", stream.raw[-400:]
    assert {f.data["code"] for f in stream.of("error")} == {"access_revoked"}
    assert not stream.of("token") and not stream.of("citation")
    assert "itemised receipt" not in stream.raw


def test_deactivation_during_an_active_run_blocks_publication(
    m4: M4, expense_doc: str, restore_finance_reader: None
) -> None:
    workflow = m4.client.app.state.report_runner.documents_workflow  # type: ignore[attr-defined]

    def before(name: str, _: Any) -> None:
        if name == "publish":
            remove(m4, "deactivate")

    workflow.deps.before_node = before
    try:
        with m4.client.stream(
            "POST",
            "/api/v1/chat",
            json={"question": POLICY_Q, "mode": "documents"},
            headers=auth(m4.token(FINANCE)),
        ) as response:
            raw = "".join(response.iter_text())
            run_id = response.headers["x-run-id"]
    finally:
        workflow.deps.before_node = None
    # The stream itself stops once the session's user is inactive; the run row shows
    # that publication was refused rather than merely not streamed.
    assert "itemised receipt" not in raw
    run = owner_exec(
        m4.db, "SELECT status, outcome_code, result FROM report_runs WHERE id = :r", {"r": run_id}
    ).one()
    assert run.status == "failed" and run.result is None


@pytest.mark.parametrize("kind", ["group", "role", "clearance"])
def test_removal_hides_published_answers_on_reopen_and_replay(
    m4: M4, expense_doc: str, kind: str, restore_finance_reader: None
) -> None:
    token = m4.token(FINANCE)
    stream = ask(m4.client, token, POLICY_Q, mode="documents")
    cid = stream.of("citation")[0].data["citation_id"]
    opened = get(m4, token, f"/runs/{stream.run_id}/citations/{cid}")
    assert opened.status_code == 200
    remove(m4, kind)
    assert get(m4, token, f"/runs/{stream.run_id}/citations/{cid}").status_code == 404
    again = replay(m4.client, token, str(stream.run_id))
    assert again.of("error")[-1].data["code"] == "access_revoked"
    assert not again.of("token") and not again.of("citation")
    run = get(m4, token, f"/runs/{stream.run_id}").json()
    assert run["result"] is None
    assert "itemised receipt" not in again.raw + str(run)


# -- audit activity --------------------------------------------------------------------


def test_audit_activity_is_scoped(m4: M4, expense_doc: str) -> None:
    tenant_wide = get(m4, m4.token(ADMIN), "/admin/audit?limit=200").json()["events"]
    assert "auth.login" in {e["event_type"] for e in tenant_wide}
    uploads = get(m4, m4.token(ADMIN), f"/admin/audit?resource_id={expense_doc}").json()
    assert "document.upload" in {e["event_type"] for e in uploads["events"]}
    scoped_uploads = get(m4, m4.token(DEPT_ADMIN), f"/admin/audit?resource_id={expense_doc}")
    assert "document.upload" in {e["event_type"] for e in scoped_uploads.json()["events"]}
    scoped = get(m4, m4.token(DEPT_ADMIN), "/admin/audit?limit=200").json()["events"]
    assert scoped
    it_admin = m4.user_email(ADMIN)  # access-admin belongs to the it department
    for event in scoped:
        assert (
            event["resource_department"] == "finance"
            or event["details"].get("department") == "finance"
            or event["actor"] != it_admin
        )
    assert all(e["actor"] != it_admin or e["resource_department"] == "finance" for e in scoped)
    assert get(m4, m4.token(FINANCE), "/admin/audit").status_code == 403
    iso = get(m4, m4.token("isolation-test/finance-admin"), "/admin/audit?limit=200").json()
    assert all(e["resource_id"] != expense_doc for e in iso["events"])
    assert m4.user_email(FINANCE) not in str(iso)
    page = get(m4, m4.token(ADMIN), "/admin/audit?limit=2").json()
    assert len(page["events"]) == 2 and page["next_before"]
    following = m4.client.get(
        "/api/v1/admin/audit",
        params={"limit": 2, "before": page["next_before"]},
        headers=auth(m4.token(ADMIN)),
    )
    assert following.status_code == 200
    assert following.json()["events"][0]["occurred_at"] < page["events"][0]["occurred_at"]
    assert "itemised receipt" not in str(tenant_wide)  # audit never carries content


def test_admin_changes_are_audited(m4: M4, restore_finance_reader: None) -> None:
    remove(m4, "group")
    events = get(m4, m4.token(ADMIN), "/admin/audit?event_type=admin.user_groups").json()
    latest = events["events"][0]
    assert latest["actor"] == m4.user_email(ADMIN)
    assert latest["details"]["removed"] == ["finance-team"]


# -- role definitions cannot lock the editor out -------------------------------------------


@pytest.fixture
def access_admin_role(m4: M4) -> Iterator[None]:
    yield
    # Restore the role first, while any preserving assignment still exists.
    response = m4.client.put(
        "/api/v1/admin/roles/access_admin",
        json={
            "name": "Tenant access administrator",
            "capabilities": ["resource.admin", "identity.admin", "audit.read", "tenant.config"],
        },
        headers=auth(m4.token(ADMIN)),
    )
    assert response.status_code == 200, response.text
    owner_exec(
        m4.db,
        "DELETE FROM user_roles WHERE user_id = :u AND role_id IN"
        " (SELECT id FROM roles WHERE key IN ('identity_keeper', 'department_admin'))",
        {"u": uid(m4, ADMIN)},
    )


def assign_directly(m4: M4, role: str, scope: str | None) -> None:
    tenant = m4.db.foundation.tenants["niletech"]
    owner_exec(
        m4.db,
        "INSERT INTO user_roles (tenant_id, user_id, role_id, scope_department)"
        " SELECT :t, :u, id, :d FROM roles WHERE tenant_id = :t AND key = :k",
        {"t": tenant.id, "u": uid(m4, ADMIN), "d": scope, "k": role},
    )


def drop_identity_admin(m4: M4) -> Any:
    return m4.client.put(
        "/api/v1/admin/roles/access_admin",
        json={
            "name": "Tenant access administrator",
            "capabilities": ["resource.admin", "audit.read", "tenant.config"],
        },
        headers=auth(m4.token(ADMIN)),
    )


def test_role_edit_that_removes_own_identity_admin_is_rejected(
    m4: M4, access_admin_role: None
) -> None:
    response = drop_identity_admin(m4)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cannot_remove_own_admin"
    # A department-scoped identity.admin does not preserve tenant-wide rights.
    assign_directly(m4, "department_admin", "finance")
    assert drop_identity_admin(m4).status_code == 422
    roles = {r["key"]: r for r in get(m4, m4.token(ADMIN), "/admin/roles").json()}
    assert "identity.admin" in roles["access_admin"]["capabilities"]
    assert get(m4, m4.token(ADMIN), "/admin/users").status_code == 200


def test_role_edit_is_allowed_when_another_assignment_preserves_rights(
    m4: M4, access_admin_role: None
) -> None:
    keeper = m4.client.put(
        "/api/v1/admin/roles/identity_keeper",
        json={"name": "Identity keeper", "capabilities": ["identity.admin"]},
        headers=auth(m4.token(ADMIN)),
    )
    assert keeper.status_code == 200
    assign_directly(m4, "identity_keeper", None)
    response = drop_identity_admin(m4)
    assert response.status_code == 200, response.text
    assert get(m4, m4.token(ADMIN), "/admin/users").status_code == 200  # still an admin
