"""Upgrading to 0009 fails closed for Drive access granted under earlier rules.

At 0007 a Drive group share became a group grant without Google membership
verification; at 0008 it became a plain user grant that no longer followed internal
membership. Neither can be told apart from a valid grant, so after the upgrade every
connector source must be denied until a sync under the new rules. M3 upload documents
with group grants keep working. The access decision is checked with the application's
own loader and the reference evaluator, before any post-upgrade sync.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.db.engine import create_app_engine, tenant_transaction
from app.documents import access
from tests.integration.conftest import DbHarness, disposable_database, migrate, owner_exec


def resource(db: DbHarness, *, source_id: uuid.UUID | None, key: str) -> uuid.UUID:
    tenant = db.foundation.tenants["niletech"]
    rid = uuid.uuid4()
    owner_exec(
        db,
        "INSERT INTO resources (id, tenant_id, kind, external_ref, title, classification,"
        " department, department_restricted, allowed_countries, source_id)"
        " VALUES (:id, :t, 'document', :ref, :title, 'internal', 'finance', false,"
        " ARRAY['*'], :s)",
        {"id": rid, "t": tenant.id, "ref": f"upgrade:{key}", "title": key, "s": source_id},
    )
    return rid


def grant(
    db: DbHarness, rid: uuid.UUID, *, group: str | None = None, user: str | None = None
) -> None:
    tenant = db.foundation.tenants["niletech"]
    owner_exec(
        db,
        "INSERT INTO resource_grants (tenant_id, resource_id, user_id, group_id, effect, action)"
        " VALUES (:t, :r, :u, :g, 'allow', 'resource.read')",
        {
            "t": tenant.id,
            "r": rid,
            "u": tenant.users[user].id if user else None,
            "g": tenant.groups[group].id if group else None,
        },
    )


def current_source(db: DbHarness) -> uuid.UUID:
    tenant = db.foundation.tenants["niletech"]
    sid = uuid.uuid4()
    owner_exec(
        db,
        "INSERT INTO connector_sources (id, tenant_id, kind, name, external_root, department,"
        " classification, allowed_countries, permissions_current, last_verified_sync_at,"
        " last_sync_status) VALUES (:id, :t, 'google_drive', 'pre-upgrade', :root, 'finance',"
        " 'internal', ARRAY['*'], true, now(), 'succeeded')",
        {"id": sid, "t": tenant.id, "root": "preUpgradeRoot" + sid.hex[:6]},
    )
    owner_exec(
        db,
        "INSERT INTO drive_items (tenant_id, source_id, external_id, name, mime_type, path,"
        " permissions_fingerprint, state) VALUES (:t, :s, 'preUpgradeFile1', 'f.md',"
        " 'text/markdown', '/f.md', 'old-fingerprint', 'active')",
        {"t": tenant.id, "s": sid},
    )
    return sid


def decision(db: DbHarness, rid: uuid.UUID) -> Any:
    tenant = db.foundation.tenants["niletech"]
    engine = create_app_engine(db.app_url, pool_size=1)
    try:
        with tenant_transaction(engine, tenant.id) as conn:
            return access.can_read(conn, tenant.users["finance-reader"].id, rid)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("revision", "drive_grant"),
    [
        ("0007_drive_admin_config", {"group": "finance-team"}),  # unverified group grant
        ("0008_sync_fencing", {"user": "finance-reader"}),  # group-derived user grant
    ],
)
def test_upgrade_denies_drive_access_until_resync(
    revision: str, drive_grant: dict[str, str]
) -> None:
    with disposable_database(seed=True, revision=revision) as db:
        source_id = current_source(db)
        drive_doc = resource(db, source_id=source_id, key="drive")
        grant(db, drive_doc, **drive_grant)
        upload_doc = resource(db, source_id=None, key="upload")
        grant(db, upload_doc, group="finance-team")  # M3 upload-group behaviour

        migrate(db, "head")

        assert decision(db, upload_doc).allowed  # upload groups are untouched
        denied = decision(db, drive_doc)
        assert not denied.allowed and denied.reason.value == "deny.unknown_input"
        row = owner_exec(
            db,
            "SELECT permissions_current, last_error_code FROM connector_sources WHERE id = :s",
            {"s": source_id},
        ).one()
        assert (row.permissions_current, row.last_error_code) == (False, "upgrade_resync_required")
        fp = owner_exec(
            db,
            "SELECT permissions_fingerprint FROM drive_items WHERE source_id = :s",
            {"s": source_id},
        ).scalar_one()
        assert fp is None  # the next sync rewrites every item under the new rules
        groups_left = owner_exec(
            db,
            "SELECT count(*) FROM resource_grants WHERE resource_id = :r AND group_id IS NOT NULL",
            {"r": drive_doc},
        ).scalar_one()
        assert groups_left == 0
        with db.owner_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0010_drive_group_grant_key"
            )


def test_upgrade_from_0009_keys_group_grants_by_google_email() -> None:
    """A database already at 0009 keeps its rows and can then hold one row per Google
    group email for the same reader and internal group."""

    with disposable_database(seed=True, revision="0009_drive_group_grants") as db:
        tenant = db.foundation.tenants["niletech"]
        doc = resource(db, source_id=None, key="keyed")
        params = {
            "t": tenant.id,
            "r": doc,
            "u": tenant.users["finance-reader"].id,
            "g": tenant.groups["finance-team"].id,
        }
        insert = (
            "INSERT INTO drive_group_grants (tenant_id, resource_id, user_id, group_id,"
            " external_email) VALUES (:t, :r, :u, :g, :e)"
        )
        owner_exec(db, insert, {**params, "e": "a@drive.example"})
        migrate(db, "head")
        owner_exec(db, insert, {**params, "e": "b@drive.example"})  # no key collision now
        emails = (
            owner_exec(
                db,
                "SELECT external_email FROM drive_group_grants WHERE resource_id = :r ORDER BY 1",
                {"r": doc},
            )
            .scalars()
            .all()
        )
        assert emails == ["a@drive.example", "b@drive.example"]
