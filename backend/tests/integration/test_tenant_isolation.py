"""Row-Level Security with the real application role on real PostgreSQL."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.db.engine import contextless_transaction, tenant_transaction
from tests.integration.conftest import DbHarness, owner_exec

TENANT_TABLES = (
    "users",
    "roles",
    "role_capabilities",
    "user_roles",
    "groups",
    "group_memberships",
    "resources",
    "resource_grants",
    "sessions",
    # M4 (connector tables are covered with real rows in test_m4_drive_sync.py)
    "tenant_settings",
)


def tenant_ids(test_db: DbHarness) -> tuple[uuid.UUID, uuid.UUID]:
    return test_db.foundation.tenants["niletech"].id, test_db.foundation.tenants[
        "isolation-test"
    ].id


def test_app_role_is_not_privileged(test_db: DbHarness, app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles"
                " WHERE rolname = current_user"
            )
        ).one()
        owners = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                " AND tableowner = current_user"
            )
        ).scalar_one()
    assert tuple(row) == (False, False, False, False)
    assert owners == 0


def test_rls_enabled_on_every_tenant_table(test_db: DbHarness) -> None:
    rows = owner_rows(
        test_db,
        "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:names)",
        {"names": [*TENANT_TABLES, "tenants", "audit_events"]},
    )
    assert len(rows) == len(TENANT_TABLES) + 2
    assert all(r.relrowsecurity for r in rows)


def owner_rows(test_db: DbHarness, sql: str, params: dict[str, object]) -> list[Any]:
    with test_db.owner_engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_missing_context_returns_no_rows(app_engine: Engine, table: str) -> None:
    with contextless_transaction(app_engine) as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
    with contextless_transaction(app_engine) as conn:
        assert conn.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 0


@pytest.mark.parametrize("table", [t for t in TENANT_TABLES if t != "sessions"])
def test_unscoped_query_sees_only_own_tenant(
    test_db: DbHarness, app_engine: Engine, table: str
) -> None:
    """Deliberately omit any WHERE tenant_id = ... from the application query."""

    tenant_a, tenant_b = tenant_ids(test_db)
    for own, other in ((tenant_a, tenant_b), (tenant_b, tenant_a)):
        with tenant_transaction(app_engine, own) as conn:
            seen = set(
                conn.execute(text(f"SELECT DISTINCT tenant_id FROM {table}")).scalars()  # noqa: S608
            )
        assert seen == {own}
        assert other not in seen


def test_markers_of_other_tenant_invisible(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, _ = tenant_ids(test_db)
    with tenant_transaction(app_engine, tenant_a) as conn:
        titles = " ".join(conn.execute(text("SELECT title FROM resources")).scalars())
        slugs = list(conn.execute(text("SELECT slug FROM tenants")).scalars())
    assert "NILETECH-FINANCE-SECRET-7421" in titles
    assert "ISOLATION" not in titles
    assert slugs == ["niletech"]


def test_password_hash_not_readable_by_app_role(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, _ = tenant_ids(test_db)
    with pytest.raises(ProgrammingError), tenant_transaction(app_engine, tenant_a) as conn:
        conn.execute(text("SELECT password_hash FROM users"))


def test_audit_events_readable_only_inside_own_tenant(
    test_db: DbHarness, app_engine: Engine
) -> None:
    """M4 opened audit reading for the scoped admin console (ADR 0402): the app role
    sees its own tenant's rows only, and none without a tenant context."""

    tenant_a, tenant_b = tenant_ids(test_db)
    for tid in (tenant_a, tenant_b):
        owner_exec(
            test_db,
            "INSERT INTO audit_events (tenant_id, event_type, action, decision, reason)"
            " VALUES (:t, 'test.marker', 'test', 'info', 'm4')",
            {"t": tid},
        )
    with tenant_transaction(app_engine, tenant_a) as conn:
        seen = set(conn.execute(text("SELECT DISTINCT tenant_id FROM audit_events")).scalars())
    assert seen == {tenant_a}
    with contextless_transaction(app_engine) as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one() == 0


def test_cannot_insert_row_for_other_tenant(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, tenant_b = tenant_ids(test_db)
    with pytest.raises(DBAPIError), tenant_transaction(app_engine, tenant_a) as conn:
        conn.execute(
            text("INSERT INTO groups (tenant_id, key, name) VALUES (:t, 'smuggled', 'x')"),
            {"t": tenant_b},
        )


def test_cannot_update_or_delete_other_tenant_rows(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, tenant_b = tenant_ids(test_db)
    with tenant_transaction(app_engine, tenant_a) as conn:
        updated = conn.execute(
            text("UPDATE groups SET name = 'hijacked' WHERE tenant_id = :t"), {"t": tenant_b}
        ).rowcount
        deleted = conn.execute(
            text("DELETE FROM group_memberships WHERE tenant_id = :t"), {"t": tenant_b}
        ).rowcount
    assert (updated, deleted) == (0, 0)
    names = owner_rows(test_db, "SELECT name FROM groups WHERE tenant_id = :t", {"t": tenant_b})
    assert all(r.name != "hijacked" for r in names)


def test_cannot_move_row_to_other_tenant(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, tenant_b = tenant_ids(test_db)
    with pytest.raises(ProgrammingError), tenant_transaction(app_engine, tenant_a) as conn:
        conn.execute(text("UPDATE groups SET tenant_id = :b"), {"b": tenant_b})


def _cross_tenant_statements(test_db: DbHarness) -> list[tuple[str, dict[str, object]]]:
    a = test_db.foundation.tenants["niletech"]
    b = test_db.foundation.tenants["isolation-test"]
    return [
        (
            "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)",
            {"t": a.id, "g": b.groups["finance-team"].id, "u": a.users["sales-reader"].id},
        ),
        (
            "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)",
            {"t": a.id, "g": a.groups["finance-team"].id, "u": b.users["sales-reader"].id},
        ),
        (
            "INSERT INTO resource_grants (tenant_id, resource_id, user_id, effect, action)"
            " VALUES (:t, :r, :u, 'allow', 'resource.read')",
            {"t": a.id, "r": a.resources["payroll-report"].id, "u": b.users["finance-reader"].id},
        ),
        (
            "INSERT INTO resource_grants (tenant_id, resource_id, group_id, effect, action)"
            " VALUES (:t, :r, :g, 'allow', 'resource.read')",
            {"t": a.id, "r": b.resources["payroll-report"].id, "g": a.groups["all-staff"].id},
        ),
        (
            "INSERT INTO user_roles (tenant_id, user_id, role_id) VALUES (:t, :u, :r)",
            {"t": a.id, "u": a.users["sales-reader"].id, "r": b.roles["reader"].id},
        ),
    ]


def test_cross_tenant_references_rejected_even_for_owner(test_db: DbHarness) -> None:
    """Composite (tenant_id, id) foreign keys reject cross-tenant links independent of RLS."""

    for sql, params in _cross_tenant_statements(test_db):
        with pytest.raises(IntegrityError), test_db.owner_engine.begin() as conn:
            conn.execute(text(sql), params)


def test_cross_tenant_references_rejected_for_app_role(
    test_db: DbHarness, app_engine: Engine
) -> None:
    tenant_a, _ = tenant_ids(test_db)
    for sql, params in _cross_tenant_statements(test_db):
        with pytest.raises(DBAPIError), tenant_transaction(app_engine, tenant_a) as conn:
            conn.execute(text(sql), params)


def test_pooled_connection_does_not_carry_context(test_db: DbHarness) -> None:
    from app.db.engine import create_app_engine

    tenant_a, tenant_b = tenant_ids(test_db)
    engine = create_app_engine(test_db.app_url, pool_size=1)
    try:
        with tenant_transaction(engine, tenant_a, uuid.uuid4()) as conn:
            pid_1 = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            # Even a session-level SET by buggy code must not survive the checkin.
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_a)}
            )
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() > 0

        with contextless_transaction(engine) as conn:
            pid_2 = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            tenant_setting = conn.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar_one()
            user_setting = conn.execute(
                text("SELECT current_setting('app.user_id', true)")
            ).scalar_one()
            visible = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
        assert pid_1 == pid_2, "same pooled backend connection reused"
        assert tenant_setting in (None, "")
        assert user_setting in (None, "")
        assert visible == 0

        with tenant_transaction(engine, tenant_b) as conn:
            pid_3 = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            seen = set(conn.execute(text("SELECT DISTINCT tenant_id FROM users")).scalars())
        assert pid_3 == pid_1
        assert seen == {tenant_b}
    finally:
        engine.dispose()


def test_context_is_transaction_scoped(test_db: DbHarness, app_engine: Engine) -> None:
    tenant_a, _ = tenant_ids(test_db)
    with app_engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_a)})
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() > 0
        with conn.begin():
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_login_lookup_function_is_exact_and_minimal(test_db: DbHarness, app_engine: Engine) -> None:
    user = test_db.foundation.tenants["niletech"].users["finance-reader"]
    with contextless_transaction(app_engine) as conn:
        cols = conn.execute(
            text("SELECT * FROM auth_lookup_login('niletech', :e)"), {"e": user.email}
        )
        assert list(cols.keys()) == [
            "tenant_id",
            "tenant_status",
            "user_id",
            "user_status",
            "password_hash",
        ]
        assert len(cols.all()) == 1
        # Wrong tenant for an existing email, wildcard and partial matches return nothing.
        for slug, email in (
            ("isolation-test", user.email),
            ("niletech", "%"),
            ("niletech", user.email.split("@")[0]),
            ("%", user.email),
        ):
            rows = conn.execute(
                text("SELECT * FROM auth_lookup_login(:s, :e)"), {"s": slug, "e": email}
            ).all()
            assert rows == []
        # The lookup grants no wider access: tenant tables are still empty here.
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_app_role_cannot_create_tenants(app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError), contextless_transaction(app_engine) as conn:
        conn.execute(text("INSERT INTO tenants (slug, name) VALUES ('rogue', 'Rogue')"))
