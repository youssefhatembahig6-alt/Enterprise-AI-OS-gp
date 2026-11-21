"""Resource metadata and grants (metadata only; no content in M0)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from app.security.policy import Grant, ResourceMeta

# M4 (ADR 0401): a resource imported from a connector source has current permissions
# only while the source is active, its connector is enabled for the tenant, the last
# sync verified every item's permissions and that verification is not older than the
# tenant's staleness bound. Every content path (eligibility, reads, publication,
# citation reopen, replay) loads resources through these two functions.
SOURCE_JOIN_SQL = """FROM resources r
              LEFT JOIN connector_sources cs ON cs.tenant_id = r.tenant_id AND cs.id = r.source_id
              LEFT JOIN tenant_settings ts ON ts.tenant_id = r.tenant_id"""
PERMISSIONS_CURRENT_SQL = """(r.permission_state = 'verified' AND (r.source_id IS NULL OR (
                       cs.status = 'active' AND cs.permissions_current
                       AND cs.kind = ANY (ts.enabled_connectors)
                       AND cs.last_verified_sync_at
                           > now() - make_interval(secs => ts.max_staleness_seconds))))
                   AS permissions_current"""


# M4 (ADR 0401): a Drive group share becomes an allow only while the Google group is
# still linked to the internal group *and* the user is still in that internal group.
# Removing either inside the application takes effect at the next authorization point.
DRIVE_GROUP_GRANTS_SQL = """
    SELECT dg.resource_id, dg.user_id FROM drive_group_grants dg
      JOIN external_group_links l
        ON l.tenant_id = dg.tenant_id AND l.provider = 'google'
       AND l.external_email = dg.external_email AND l.group_id = dg.group_id
      JOIN group_memberships gm
        ON gm.tenant_id = dg.tenant_id AND gm.group_id = dg.group_id AND gm.user_id = dg.user_id
     WHERE dg.resource_id = ANY(:ids)
"""


def _drive_group_grants(conn: Connection, ids: list[UUID]) -> dict[UUID, list[Grant]]:
    out: dict[UUID, list[Grant]] = {}
    for row in conn.execute(text(DRIVE_GROUP_GRANTS_SQL), {"ids": ids}).all():
        out.setdefault(row.resource_id, []).append(
            Grant(effect="allow", action="resource.read", user_id=row.user_id)
        )
    return out


def load_resource_meta(conn: Connection, resource_id: UUID) -> ResourceMeta | None:
    row = conn.execute(
        text(
            "SELECT r.id, r.tenant_id, r.classification, r.department,"
            " r.department_restricted, r.allowed_countries, r.status, "
            + PERMISSIONS_CURRENT_SQL
            + " "
            + SOURCE_JOIN_SQL
            + " WHERE r.id = :rid"
        ),
        {"rid": resource_id},
    ).one_or_none()
    if row is None:
        return None
    grants = conn.execute(
        text(
            "SELECT effect, action, user_id, group_id FROM resource_grants"
            " WHERE tenant_id = :tid AND resource_id = :rid"
        ),
        {"tid": row.tenant_id, "rid": resource_id},
    ).all()
    derived = _drive_group_grants(conn, [resource_id]).get(resource_id, [])
    return ResourceMeta(
        tenant_id=row.tenant_id,
        resource_id=row.id,
        classification=row.classification,
        department=row.department,
        department_restricted=row.department_restricted,
        allowed_countries=tuple(row.allowed_countries),
        active=row.status == "active",
        permissions_verified=bool(row.permissions_current),
        grants=tuple(
            Grant(effect=g.effect, action=g.action, user_id=g.user_id, group_id=g.group_id)
            for g in grants
        )
        + tuple(derived),
    )


def load_resource_metas(conn: Connection, resource_ids: list[UUID]) -> dict[UUID, ResourceMeta]:
    """Batch form of ``load_resource_meta`` (two queries) for eligibility evaluation."""

    if not resource_ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT r.id, r.tenant_id, r.classification, r.department,"
            " r.department_restricted, r.allowed_countries, r.status, "
            + PERMISSIONS_CURRENT_SQL
            + " "
            + SOURCE_JOIN_SQL
            + " WHERE r.id = ANY(:ids)"
        ),
        {"ids": resource_ids},
    ).all()
    grants: dict[UUID, list[Grant]] = {}
    for g in conn.execute(
        text(
            "SELECT resource_id, effect, action, user_id, group_id FROM resource_grants"
            " WHERE resource_id = ANY(:ids)"
        ),
        {"ids": resource_ids},
    ).all():
        grants.setdefault(g.resource_id, []).append(
            Grant(effect=g.effect, action=g.action, user_id=g.user_id, group_id=g.group_id)
        )
    for rid, derived in _drive_group_grants(conn, resource_ids).items():
        grants.setdefault(rid, []).extend(derived)
    return {
        row.id: ResourceMeta(
            tenant_id=row.tenant_id,
            resource_id=row.id,
            classification=row.classification,
            department=row.department,
            department_restricted=row.department_restricted,
            allowed_countries=tuple(row.allowed_countries),
            active=row.status == "active",
            permissions_verified=bool(row.permissions_current),
            grants=tuple(grants.get(row.id, ())),
        )
        for row in rows
    }
