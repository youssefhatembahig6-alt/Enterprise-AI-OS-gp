"""Scoped administration (ADR 0402): documents, sources, users, roles and audit activity.

Scope rules, all enforced here (the browser only hides what the backend refuses):

* Every admin capability is a role capability with an optional department scope
  (``user_roles.scope_department``); ``None`` is tenant-wide.
* ``resource.admin`` — documents and connector sources whose department is covered.
  Metadata only: it never implies reading content.
* ``identity.admin`` — users whose department is covered. A scoped administrator may
  only assign roles *with a scope inside their own scope*, and nobody may assign a
  role carrying an *administrative* capability they do not hold for that scope (no
  escalation; content capabilities grant nothing without resource grants). Group
  membership, group links and role definitions are tenant-wide (``scope None``)
  operations, because groups are tenant-wide.
* ``audit.read`` — tenant-wide sees every tenant event; a scoped reader sees events on
  resources and sources of covered departments and events by users of those departments.
* ``tenant.config`` — tenant-wide only: packs, connectors and the sync schedule.

Objects outside the caller's scope are reported as not found (404), so scope does not
reveal other departments' users, documents or sources. Missing capability is 403.
Every change is audited in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.connectors.drive import ConnectorConfig
from app.connectors.sync import SyncOutcome
from app.db.engine import tenant_transaction
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext
from app.security.policy import (
    CLASSIFICATION_RANK,
    Capability,
    capability_scope,
    covers,
)
from app.tenancy.config import require_connector, update_settings

ROLE_KEY = r"^[a-z][a-z0-9_]{0,62}$"
ADMIN_CAPABILITIES = frozenset(
    {
        Capability.RESOURCE_ADMIN.value,
        Capability.IDENTITY_ADMIN.value,
        Capability.AUDIT_READ.value,
        Capability.TENANT_CONFIG.value,
    }
)
DEPARTMENT_RE = r"^[a-z][a-z0-9_-]{0,62}$"


class AdminForbidden(Exception):
    """The caller lacks the capability (or the scope) for this operation."""


class AdminNotFound(Exception):
    """Unknown or outside the caller's scope (uniform 404)."""


class AdminInvalid(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


SyncRunner = Callable[[UUID, UUID, UUID, UUID], SyncOutcome | None]


def _require(ctx: AccessContext, cap: Capability) -> frozenset[str] | None:
    scope = capability_scope(ctx.subject, cap)
    if scope is not None and not scope:
        raise AdminForbidden
    return scope


def _require_tenant_wide(ctx: AccessContext, cap: Capability) -> None:
    if _require(ctx, cap) is not None:
        raise AdminForbidden


def _in_scope(scope: frozenset[str] | None, department: str | None) -> bool:
    return scope is None or (department is not None and department in scope)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


_USERS_ALL = (
    "SELECT u.id, u.email, u.display_name, u.status, u.clearance, u.country,"
    " u.department, u.updated_at FROM users u ORDER BY u.email"
)
_USERS_ONE = (
    "SELECT u.id, u.email, u.display_name, u.status, u.clearance, u.country,"
    " u.department, u.updated_at FROM users u WHERE u.id = :uid"
)


class AdminService:
    def __init__(
        self,
        engine: Engine,
        drive_config: ConnectorConfig,
        run_sync: SyncRunner | None,
    ) -> None:
        self._engine = engine
        self._drive_config = drive_config
        self._run_sync = run_sync

    def _tx(self, ctx: AccessContext) -> Any:
        return tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id)

    @staticmethod
    def _audit(
        conn: Connection,
        ctx: AccessContext,
        event_type: str,
        reason: str,
        *,
        resource_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        action: str = "admin",
    ) -> None:
        record_audit_event(
            conn,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type=event_type,
            action=action,
            resource_id=resource_id,
            decision="allow",
            reason=reason,
            details=details or {},
        )

    # -- tenant configuration ------------------------------------------------------------

    def tenant_settings(self, ctx: AccessContext) -> dict[str, Any]:
        from app.tenancy.config import load_settings  # noqa: PLC0415

        with self._tx(ctx) as conn:
            return load_settings(conn).as_dict()

    def update_tenant_settings(
        self,
        ctx: AccessContext,
        packs: list[str] | None,
        connectors: list[str] | None,
        sync_mode: str | None,
    ) -> dict[str, Any]:
        _require_tenant_wide(ctx, Capability.TENANT_CONFIG)
        with self._tx(ctx) as conn:
            try:
                updated = update_settings(
                    conn,
                    tenant_id=ctx.tenant_id,
                    packs=packs,
                    connectors=connectors,
                    sync_mode=sync_mode,
                    updated_by=ctx.user_id,
                )
            except ValueError as exc:
                raise AdminInvalid("invalid_settings", str(exc)) from exc
            self._audit(
                conn,
                ctx,
                "admin.tenant_settings",
                "settings_updated",
                action="tenant.config",
                details={
                    "packs": sorted(updated.enabled_packs),
                    "connectors": sorted(updated.enabled_connectors),
                    "sync_mode": updated.sync_mode,
                    "version": updated.version,
                },
            )
            return updated.as_dict()

    # -- users ---------------------------------------------------------------------------

    def _user_rows(self, conn: Connection, user_id: UUID | None = None) -> list[dict[str, Any]]:
        rows = conn.execute(
            text(_USERS_ONE if user_id else _USERS_ALL),
            {"uid": user_id} if user_id else {},
        ).all()
        roles: dict[UUID, list[dict[str, Any]]] = {}
        for r in conn.execute(
            text(
                "SELECT ur.user_id, r.key, ur.scope_department FROM user_roles ur"
                " JOIN roles r ON r.tenant_id = ur.tenant_id AND r.id = ur.role_id"
                " ORDER BY r.key, ur.scope_department NULLS FIRST"
            )
        ).all():
            roles.setdefault(r.user_id, []).append(
                {"role": r.key, "scope_department": r.scope_department}
            )
        groups: dict[UUID, list[str]] = {}
        for g in conn.execute(
            text(
                "SELECT gm.user_id, g.key FROM group_memberships gm"
                " JOIN groups g ON g.tenant_id = gm.tenant_id AND g.id = gm.group_id ORDER BY g.key"
            )
        ).all():
            groups.setdefault(g.user_id, []).append(g.key)
        return [
            {
                "user_id": str(row.id),
                "email": row.email,
                "display_name": row.display_name,
                "status": row.status,
                "clearance": row.clearance,
                "country": row.country,
                "department": row.department,
                "roles": roles.get(row.id, []),
                "groups": groups.get(row.id, []),
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]

    def list_users(self, ctx: AccessContext) -> list[dict[str, Any]]:
        scope = _require(ctx, Capability.IDENTITY_ADMIN)
        with self._tx(ctx) as conn:
            return [u for u in self._user_rows(conn) if _in_scope(scope, u["department"])]

    def _target(self, conn: Connection, scope: frozenset[str] | None, user_id: UUID) -> Any:
        row = conn.execute(
            text("SELECT id, department, status FROM users WHERE id = :u FOR UPDATE"),
            {"u": user_id},
        ).one_or_none()
        if row is None or not _in_scope(scope, row.department):
            raise AdminNotFound
        return row

    def update_user(
        self, ctx: AccessContext, user_id: UUID, changes: dict[str, Any]
    ) -> dict[str, Any]:
        scope = _require(ctx, Capability.IDENTITY_ADMIN)
        allowed = {"status", "clearance", "country", "department", "display_name"}
        changes = {k: v for k, v in changes.items() if k in allowed}
        if not changes:
            raise AdminInvalid("no_changes", "Nothing to change.")
        if "status" in changes and changes["status"] not in ("active", "inactive"):
            raise AdminInvalid("invalid_status", "Status must be active or inactive.")
        if changes.get("clearance") is not None and changes["clearance"] not in CLASSIFICATION_RANK:
            raise AdminInvalid("invalid_clearance", "Unknown clearance.")
        with self._tx(ctx) as conn:
            self._target(conn, scope, user_id)
            if user_id == ctx.user_id and changes.get("status") == "inactive":
                raise AdminInvalid("cannot_deactivate_self", "You cannot deactivate yourself.")
            if "department" in changes and not _in_scope(scope, changes["department"]):
                raise AdminForbidden
            sets = ", ".join(f"{k} = :{k}" for k in sorted(changes))
            try:
                conn.execute(
                    text(f"UPDATE users SET {sets}, updated_at = now() WHERE id = :uid"),  # noqa: S608
                    {**changes, "uid": user_id},
                )
            except Exception as exc:  # CHECK constraint (country/department format)
                raise AdminInvalid("invalid_user", "A value has an invalid format.") from exc
            self._audit(
                conn,
                ctx,
                "admin.user",
                "user_updated",
                action="identity.admin",
                details={"target_user_id": str(user_id), "fields": sorted(changes)},
            )
            return self._user_rows(conn, user_id)[0]

    def _role_caps(self, conn: Connection) -> dict[str, tuple[UUID, frozenset[str]]]:
        out: dict[str, tuple[UUID, set[str]]] = {}
        for r in conn.execute(
            text(
                "SELECT r.id, r.key, rc.capability FROM roles r LEFT JOIN role_capabilities rc"
                " ON rc.tenant_id = r.tenant_id AND rc.role_id = r.id"
            )
        ).all():
            entry = out.setdefault(r.key, (r.id, set()))
            if r.capability:
                entry[1].add(r.capability)
        return {k: (v[0], frozenset(v[1])) for k, v in out.items()}

    def _may_assign(
        self,
        ctx: AccessContext,
        scope: frozenset[str] | None,
        caps: frozenset[str],
        dept: str | None,
    ) -> bool:
        """A scoped administrator assigns only inside their scope. Administrative
        capabilities additionally need the assigner to hold them for that scope (no
        escalation). Content capabilities grant nothing without resource grants."""

        if scope is not None and (dept is None or dept not in scope):
            return False
        return all(
            covers(ctx.subject, Capability(c), dept) for c in caps if c in ADMIN_CAPABILITIES
        )

    def set_user_roles(
        self, ctx: AccessContext, user_id: UUID, assignments: list[tuple[str, str | None]]
    ) -> dict[str, Any]:
        scope = _require(ctx, Capability.IDENTITY_ADMIN)
        if user_id == ctx.user_id:
            raise AdminInvalid("cannot_change_own_roles", "You cannot change your own roles.")
        with self._tx(ctx) as conn:
            self._target(conn, scope, user_id)
            roles = self._role_caps(conn)
            wanted: set[tuple[str, str | None]] = set()
            for key, dept in assignments:
                if key not in roles:
                    raise AdminInvalid("unknown_role", "A role in the list does not exist.")
                wanted.add((key, dept))
            current = {
                (r.key, r.scope_department)
                for r in conn.execute(
                    text(
                        "SELECT r.key, ur.scope_department FROM user_roles ur JOIN roles r"
                        " ON r.tenant_id = ur.tenant_id AND r.id = ur.role_id WHERE ur.user_id = :u"
                    ),
                    {"u": user_id},
                ).all()
            }
            for key, dept in wanted ^ current:  # added or removed: both need the power
                if not self._may_assign(ctx, scope, roles[key][1], dept):
                    raise AdminForbidden
            for key, dept in current - wanted:
                conn.execute(
                    text(
                        "DELETE FROM user_roles WHERE user_id = :u AND role_id = :r"
                        " AND scope_department IS NOT DISTINCT FROM :d"
                    ),
                    {"u": user_id, "r": roles[key][0], "d": dept},
                )
            for key, dept in wanted - current:
                try:
                    conn.execute(
                        text(
                            "INSERT INTO user_roles (tenant_id, user_id, role_id, scope_department)"
                            " VALUES (:t, :u, :r, :d)"
                        ),
                        {"t": ctx.tenant_id, "u": user_id, "r": roles[key][0], "d": dept},
                    )
                except Exception as exc:
                    raise AdminInvalid("invalid_scope", "Invalid department scope.") from exc
            self._audit(
                conn,
                ctx,
                "admin.user_roles",
                "roles_set",
                action="identity.admin",
                details={
                    "target_user_id": str(user_id),
                    "added": sorted(f"{k}@{d or '*'}" for k, d in wanted - current),
                    "removed": sorted(f"{k}@{d or '*'}" for k, d in current - wanted),
                },
            )
            return self._user_rows(conn, user_id)[0]

    def set_user_groups(
        self, ctx: AccessContext, user_id: UUID, group_keys: list[str]
    ) -> dict[str, Any]:
        _require_tenant_wide(ctx, Capability.IDENTITY_ADMIN)
        with self._tx(ctx) as conn:
            self._target(conn, None, user_id)
            groups = {g.key: g.id for g in conn.execute(text("SELECT id, key FROM groups")).all()}
            if unknown := [k for k in group_keys if k not in groups]:
                raise AdminInvalid("unknown_group", f"Unknown group: {', '.join(sorted(unknown))}")
            current = {
                g.key
                for g in conn.execute(
                    text(
                        "SELECT g.key FROM group_memberships gm JOIN groups g"
                        " ON g.tenant_id = gm.tenant_id AND g.id = gm.group_id"
                        " WHERE gm.user_id = :u"
                    ),
                    {"u": user_id},
                ).all()
            }
            wanted = set(group_keys)
            for key in current - wanted:
                conn.execute(
                    text("DELETE FROM group_memberships WHERE user_id = :u AND group_id = :g"),
                    {"u": user_id, "g": groups[key]},
                )
            for key in wanted - current:
                conn.execute(
                    text(
                        "INSERT INTO group_memberships (tenant_id, group_id, user_id)"
                        " VALUES (:t, :g, :u)"
                    ),
                    {"t": ctx.tenant_id, "g": groups[key], "u": user_id},
                )
            self._audit(
                conn,
                ctx,
                "admin.user_groups",
                "groups_set",
                action="identity.admin",
                details={
                    "target_user_id": str(user_id),
                    "added": sorted(wanted - current),
                    "removed": sorted(current - wanted),
                },
            )
            return self._user_rows(conn, user_id)[0]

    # -- roles and groups ------------------------------------------------------------------

    def list_roles(self, ctx: AccessContext) -> list[dict[str, Any]]:
        _require(ctx, Capability.IDENTITY_ADMIN)
        with self._tx(ctx) as conn:
            names = {r.key: r.name for r in conn.execute(text("SELECT key, name FROM roles")).all()}
            return [
                {"key": key, "name": names[key], "capabilities": sorted(caps)}
                for key, (_, caps) in sorted(self._role_caps(conn).items())
            ]

    def upsert_role(
        self, ctx: AccessContext, key: str, name: str, capabilities: list[str]
    ) -> dict[str, Any]:
        import re  # noqa: PLC0415

        _require_tenant_wide(ctx, Capability.IDENTITY_ADMIN)
        if not re.match(ROLE_KEY, key):
            raise AdminInvalid("invalid_role_key", "Role keys are lowercase letters, digits, _.")
        known = {c.value for c in Capability}
        if unknown := sorted(set(capabilities) - known):
            raise AdminInvalid("unknown_capability", f"Unknown capability: {', '.join(unknown)}")
        if not all(covers(ctx.subject, Capability(c), None) for c in capabilities):
            raise AdminForbidden  # cannot define a role with more power than you hold
        with self._tx(ctx) as conn:
            role_id = conn.execute(text("SELECT id FROM roles WHERE key = :k"), {"k": key}).scalar()
            if role_id is None:
                role_id = uuid.uuid4()
                conn.execute(
                    text("INSERT INTO roles (id, tenant_id, key, name) VALUES (:id, :t, :k, :n)"),
                    {"id": role_id, "t": ctx.tenant_id, "k": key, "n": name[:200]},
                )
            else:
                current = self._role_caps(conn)[key][1]
                if not all(covers(ctx.subject, Capability(c), None) for c in current):
                    raise AdminForbidden
                self._keeps_own_identity_admin(conn, ctx, key, frozenset(capabilities))
                conn.execute(
                    text("UPDATE roles SET name = :n WHERE id = :id"),
                    {"n": name[:200], "id": role_id},
                )
                conn.execute(
                    text("DELETE FROM role_capabilities WHERE role_id = :r"), {"r": role_id}
                )
            for cap in sorted(set(capabilities)):
                conn.execute(
                    text(
                        "INSERT INTO role_capabilities (tenant_id, role_id, capability)"
                        " VALUES (:t, :r, :c)"
                    ),
                    {"t": ctx.tenant_id, "r": role_id, "c": cap},
                )
            self._audit(
                conn,
                ctx,
                "admin.role",
                "role_saved",
                action="identity.admin",
                details={"role": key, "capabilities": sorted(set(capabilities))},
            )
        return {"key": key, "name": name[:200], "capabilities": sorted(set(capabilities))}

    def _keeps_own_identity_admin(
        self, conn: Connection, ctx: AccessContext, key: str, new_caps: frozenset[str]
    ) -> None:
        """Reject a role edit that would leave the caller without tenant-wide
        ``identity.admin``. Evaluated on the caller's *effective* result: all of their
        assignments, with ``key`` carrying ``new_caps``; a department-scoped assignment
        does not preserve tenant-wide rights."""

        caps = {k: v[1] for k, v in self._role_caps(conn).items()}
        caps[key] = new_caps
        assignments = conn.execute(
            text(
                "SELECT r.key, ur.scope_department FROM user_roles ur JOIN roles r"
                " ON r.tenant_id = ur.tenant_id AND r.id = ur.role_id WHERE ur.user_id = :u"
            ),
            {"u": ctx.user_id},
        ).all()
        if not any(
            a.scope_department is None and Capability.IDENTITY_ADMIN.value in caps.get(a.key, ())
            for a in assignments
        ):
            raise AdminInvalid(
                "cannot_remove_own_admin",
                "This change would remove your own identity administration rights.",
            )

    def list_groups(self, ctx: AccessContext) -> list[dict[str, Any]]:
        _require(ctx, Capability.IDENTITY_ADMIN)
        with self._tx(ctx) as conn:
            links: dict[UUID, list[str]] = {}
            for link in conn.execute(
                text("SELECT group_id, external_email FROM external_group_links ORDER BY 2")
            ).all():
                links.setdefault(link.group_id, []).append(link.external_email)
            return [
                {"key": g.key, "name": g.name, "google_groups": links.get(g.id, [])}
                for g in conn.execute(text("SELECT id, key, name FROM groups ORDER BY key")).all()
            ]

    def set_group_links(
        self, ctx: AccessContext, group_key: str, emails: list[str]
    ) -> dict[str, Any]:
        _require_tenant_wide(ctx, Capability.IDENTITY_ADMIN)
        cleaned = sorted({e.strip().lower() for e in emails if e.strip()})
        if any("@" not in e[1:] for e in cleaned):
            raise AdminInvalid("invalid_email", "Group links need group email addresses.")
        with self._tx(ctx) as conn:
            gid = conn.execute(
                text("SELECT id FROM groups WHERE key = :k"), {"k": group_key}
            ).scalar()
            if gid is None:
                raise AdminNotFound
            conn.execute(text("DELETE FROM external_group_links WHERE group_id = :g"), {"g": gid})
            for email in cleaned:
                conn.execute(
                    text(
                        "INSERT INTO external_group_links"
                        " (tenant_id, provider, external_email, group_id)"
                        " VALUES (:t, 'google', :e, :g)"
                        " ON CONFLICT (tenant_id, provider, external_email)"
                        " DO UPDATE SET group_id = EXCLUDED.group_id"
                    ),
                    {"t": ctx.tenant_id, "e": email, "g": gid},
                )
            self._audit(
                conn,
                ctx,
                "admin.group_links",
                "group_links_set",
                action="identity.admin",
                details={"group": group_key, "links": len(cleaned)},
            )
        return {"key": group_key, "google_groups": cleaned}

    # -- documents -------------------------------------------------------------------------

    def list_documents(self, ctx: AccessContext) -> list[dict[str, Any]]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        from app.repositories.resources import load_resource_metas  # noqa: PLC0415

        with self._tx(ctx) as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.id, d.title, d.source, d.current_version, d.active_generation,
                           d.created_at, r.department, r.classification, r.permission_state,
                           r.department_restricted, r.allowed_countries, r.source_id,
                           cs.name AS source_name, v.status AS version_status
                      FROM documents d
                      JOIN resources r ON r.tenant_id = d.tenant_id AND r.id = d.id
                      LEFT JOIN connector_sources cs
                             ON cs.tenant_id = r.tenant_id AND cs.id = r.source_id
                      LEFT JOIN document_versions v ON v.tenant_id = d.tenant_id
                             AND v.document_id = d.id AND v.version = d.current_version
                     WHERE d.deleted_at IS NULL
                     ORDER BY d.created_at, d.id
                    """
                )
            ).all()
            rows = [r for r in rows if _in_scope(scope, r.department)]
            metas = load_resource_metas(conn, [r.id for r in rows])
            names = {
                **{
                    g.id: f"group:{g.key}"
                    for g in conn.execute(text("SELECT id, key FROM groups")).all()
                },
                **{
                    u.id: f"user:{u.email}"
                    for u in conn.execute(text("SELECT id, email FROM users")).all()
                },
            }
            out = []
            for r in rows:
                meta = metas.get(r.id)
                out.append(
                    {
                        "document_id": str(r.id),
                        "title": r.title,
                        "source": r.source,
                        "source_id": str(r.source_id) if r.source_id else None,
                        "source_name": r.source_name,
                        "department": r.department,
                        "classification": r.classification,
                        "department_restricted": r.department_restricted,
                        "allowed_countries": list(r.allowed_countries),
                        "permission_state": r.permission_state,
                        "permissions_current": bool(meta and meta.permissions_verified),
                        "status": r.version_status,
                        "searchable": r.active_generation is not None,
                        "current_version": r.current_version,
                        "grants": sorted(
                            f"{g.effect}:{names.get(g.user_id or g.group_id, 'unknown')}"
                            for g in (meta.grants if meta else ())
                            if g.action == "resource.read"
                        ),
                        "managed_by_connector": r.source_id is not None,
                    }
                )
            return out

    # -- connector sources -------------------------------------------------------------------

    _SOURCE_VIEW = """
        SELECT s.id, s.kind, s.name, s.external_root, s.department, s.classification,
               s.department_restricted, s.allowed_countries, s.status, s.permissions_current,
               s.last_sync_started_at, s.last_sync_finished_at, s.last_verified_sync_at,
               s.last_sync_status, s.last_error_code, s.created_at,
               ts.sync_mode, ts.sync_interval_seconds, ts.max_staleness_seconds,
               (s.kind = ANY (coalesce(ts.enabled_connectors, '{}'))) AS connector_enabled,
               (s.last_verified_sync_at IS NOT NULL AND s.last_verified_sync_at
                    > now() - make_interval(secs => ts.max_staleness_seconds)) AS fresh,
               (SELECT count(*) FROM drive_items i WHERE i.source_id = s.id
                    AND i.state <> 'removed') AS item_count
          FROM connector_sources s LEFT JOIN tenant_settings ts ON ts.tenant_id = s.tenant_id
    """

    @staticmethod
    def _source_out(r: Any) -> dict[str, Any]:
        if not r.connector_enabled:
            access = "connector_disabled"
        elif r.status != "active":
            access = "paused"
        elif r.last_verified_sync_at is None:
            access = "never_verified"
        elif not r.permissions_current and r.last_error_code in (
            "policy_changed",
            "upgrade_resync_required",
        ):
            access = "resync_required"
        elif not r.permissions_current:
            access = "last_sync_failed"
        elif not r.fresh:
            access = "stale"
        else:
            access = "current"
        return {
            "source_id": str(r.id),
            "kind": r.kind,
            "name": r.name,
            "external_root": r.external_root,
            "department": r.department,
            "classification": r.classification,
            "department_restricted": r.department_restricted,
            "allowed_countries": list(r.allowed_countries),
            "status": r.status,
            # Content of this source is served only while access == "current".
            "access": access,
            "last_verified_sync_at": _iso(r.last_verified_sync_at),
            "last_sync_started_at": _iso(r.last_sync_started_at),
            "last_sync_finished_at": _iso(r.last_sync_finished_at),
            "last_sync_status": r.last_sync_status,
            "last_error_code": r.last_error_code,
            "sync_mode": r.sync_mode,
            "sync_interval_seconds": r.sync_interval_seconds,
            "max_staleness_seconds": r.max_staleness_seconds,
            "item_count": int(r.item_count),
        }

    def _load_source(self, conn: Connection, scope: frozenset[str] | None, source_id: UUID) -> Any:
        row = conn.execute(
            text(self._SOURCE_VIEW + " WHERE s.id = :id AND s.status <> 'deleted'"),
            {"id": source_id},
        ).one_or_none()
        if row is None or not _in_scope(scope, row.department):
            raise AdminNotFound
        return row

    def list_sources(self, ctx: AccessContext) -> list[dict[str, Any]]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        with self._tx(ctx) as conn:
            rows = conn.execute(
                text(self._SOURCE_VIEW + " WHERE s.status <> 'deleted' ORDER BY s.created_at")
            ).all()
            return [self._source_out(r) for r in rows if _in_scope(scope, r.department)]

    def get_source(self, ctx: AccessContext, source_id: UUID) -> dict[str, Any]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        with self._tx(ctx) as conn:
            out = self._source_out(self._load_source(conn, scope, source_id))
            out["recent_runs"] = [
                {
                    "run_id": str(r.id),
                    "trigger": r.trigger,
                    "status": r.status,
                    "started_at": _iso(r.started_at),
                    "finished_at": _iso(r.finished_at),
                    "error_code": r.error_code,
                    "stats": r.stats,
                }
                for r in conn.execute(
                    text(
                        "SELECT id, trigger, status, started_at, finished_at, error_code, stats"
                        " FROM connector_sync_runs WHERE source_id = :s"
                        " ORDER BY started_at DESC LIMIT 10"
                    ),
                    {"s": source_id},
                ).all()
            ]
            out["items"] = [
                {
                    "external_id": i.external_id,
                    "name": i.name,
                    "path": i.path,
                    "state": i.state,
                    "document_id": str(i.document_id) if i.document_id else None,
                    "unresolved_principals": i.unresolved_principals,
                    "updated_at": _iso(i.updated_at),
                }
                for i in conn.execute(
                    text(
                        "SELECT external_id, name, path, state, document_id, unresolved_principals,"
                        " updated_at FROM drive_items WHERE source_id = :s ORDER BY path"
                    ),
                    {"s": source_id},
                ).all()
            ]
            return out

    def create_source(self, ctx: AccessContext, body: dict[str, Any]) -> dict[str, Any]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        department = body["department"]
        if not _in_scope(scope, department):
            raise AdminForbidden
        if body["classification"] not in CLASSIFICATION_RANK:
            raise AdminInvalid("invalid_classification", "Unknown classification.")
        root = body["external_root"]
        with self._tx(ctx) as conn:
            require_connector(conn, "google_drive")
            config = self._drive_config.tenant(ctx.tenant_slug)  # re-read each time
            if config is None:
                raise AdminInvalid(
                    "drive_not_configured",
                    "Google Drive credentials are not configured for this organization.",
                )
            if root not in config.allowed_roots:
                raise AdminInvalid(
                    "root_not_allowed",
                    "This folder is not one the server operator allowed for this organization.",
                )
            source_id = uuid.uuid4()
            try:
                conn.execute(
                    text(
                        """
                        INSERT INTO connector_sources (id, tenant_id, kind, name, external_root,
                            department, classification, department_restricted,
                            allowed_countries, created_by)
                        VALUES (:id, :t, 'google_drive', :n, :root, :dept, :cls, :restricted,
                                :countries, :u)
                        """
                    ),
                    {
                        "id": source_id,
                        "t": ctx.tenant_id,
                        "n": body["name"][:200],
                        "root": root,
                        "dept": department,
                        "cls": body["classification"],
                        "restricted": bool(body.get("department_restricted", False)),
                        "countries": body["allowed_countries"],
                        "u": ctx.user_id,
                    },
                )
            except Exception as exc:
                raise AdminInvalid(
                    "invalid_source", "The source is invalid or already exists."
                ) from exc
            self._audit(
                conn,
                ctx,
                "admin.source",
                "source_created",
                action="resource.admin",
                details={"source_id": str(source_id), "department": department},
            )
            return self._source_out(self._load_source(conn, scope, source_id))

    def update_source(
        self, ctx: AccessContext, source_id: UUID, changes: dict[str, Any]
    ) -> dict[str, Any]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        allowed = {
            "name",
            "department",
            "classification",
            "department_restricted",
            "allowed_countries",
            "status",
        }
        changes = {k: v for k, v in changes.items() if k in allowed and v is not None}
        if "status" in changes and changes["status"] not in ("active", "paused", "deleted"):
            raise AdminInvalid("invalid_status", "Status must be active, paused or deleted.")
        if "department" in changes and not _in_scope(scope, changes["department"]):
            raise AdminForbidden
        with self._tx(ctx) as conn:
            self._load_source(conn, scope, source_id)
            if changes:
                sets = ", ".join(f"{k} = :{k}" for k in sorted(changes))
                # Attribute changes apply to items at the next verified sync; until then
                # the source is not current, so its content is denied (fail closed). The
                # policy version moves on and the claim is released, so a sync already
                # running under the old policy is fenced out of every later write.
                stale = (
                    ", permissions_current = false, policy_version = policy_version + 1,"
                    " claim_run_id = NULL, sync_claimed_until = NULL,"
                    " last_sync_status = NULL, last_error_code = 'policy_changed'"
                    if set(changes) - {"name"}
                    else ""
                )
                conn.execute(
                    text(
                        f"UPDATE connector_sources SET {sets}{stale}, updated_at = now()"  # noqa: S608
                        " WHERE id = :sid"
                    ),
                    {**changes, "sid": source_id},
                )
            self._audit(
                conn,
                ctx,
                "admin.source",
                "source_updated",
                action="resource.admin",
                details={"source_id": str(source_id), "fields": sorted(changes)},
            )
            if changes.get("status") == "deleted":
                return {"source_id": str(source_id), "status": "deleted"}
            return self._source_out(self._load_source(conn, scope, source_id))

    def sync_source(self, ctx: AccessContext, source_id: UUID) -> dict[str, Any]:
        """Create a queued run row first, so the caller can wait for *this* run (a
        previous success or an unrelated scheduled run never answers for it)."""

        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        if self._run_sync is None:
            raise AdminInvalid("sync_unavailable", "Sync is not available on this server.")
        run_id = uuid.uuid4()
        with self._tx(ctx) as conn:
            self._load_source(conn, scope, source_id)
            require_connector(conn, "google_drive")
            conn.execute(
                text(
                    "INSERT INTO connector_sync_runs (id, tenant_id, source_id, trigger,"
                    " requested_by, status) VALUES (:id, :t, :s, 'manual', :u, 'queued')"
                ),
                {"id": run_id, "t": ctx.tenant_id, "s": source_id, "u": ctx.user_id},
            )
        outcome = self._run_sync(ctx.tenant_id, source_id, ctx.user_id, run_id)
        if outcome is None:
            return {"status": "queued", "run_id": str(run_id), "error_code": None, "stats": {}}
        return {
            "status": outcome.status,
            "run_id": str(run_id),
            "error_code": outcome.error_code,
            "stats": outcome.stats,
        }

    def get_run(self, ctx: AccessContext, source_id: UUID, run_id: UUID) -> dict[str, Any]:
        scope = _require(ctx, Capability.RESOURCE_ADMIN)
        with self._tx(ctx) as conn:
            self._load_source(conn, scope, source_id)
            r = conn.execute(
                text(
                    "SELECT id, trigger, status, requested_at, started_at, finished_at,"
                    " error_code, stats, policy_version FROM connector_sync_runs"
                    " WHERE id = :r AND source_id = :s"
                ),
                {"r": run_id, "s": source_id},
            ).one_or_none()
        if r is None:
            raise AdminNotFound
        return {
            "run_id": str(r.id),
            "trigger": r.trigger,
            "status": r.status,
            "terminal": r.status in ("succeeded", "failed", "skipped", "superseded"),
            "requested_at": _iso(r.requested_at),
            "started_at": _iso(r.started_at) if r.status != "queued" else None,
            "finished_at": _iso(r.finished_at),
            "error_code": r.error_code,
            "stats": r.stats,
            "policy_version": r.policy_version,
        }

    # -- audit -----------------------------------------------------------------------------

    def audit(
        self,
        ctx: AccessContext,
        *,
        limit: int,
        before: str | None,
        event_type: str | None,
        resource_id: UUID | None,
    ) -> dict[str, Any]:
        scope = _require(ctx, Capability.AUDIT_READ)
        clauses = ["a.tenant_id = :t"]
        params: dict[str, Any] = {"t": ctx.tenant_id, "limit": limit + 1}
        if before:
            clauses.append("a.occurred_at < CAST(:before AS timestamptz)")
            params["before"] = before
        if event_type:
            clauses.append("a.event_type = :et")
            params["et"] = event_type
        if resource_id:
            clauses.append("a.resource_id = :rid")
            params["rid"] = resource_id
        if scope is not None:
            clauses.append(
                "(r.department = ANY(:depts) OR actor.department = ANY(:depts)"
                " OR a.details->>'department' = ANY(:depts))"
            )
            params["depts"] = sorted(scope)
        try:
            with self._tx(ctx) as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT a.id, a.occurred_at, a.request_id, a.event_type, a.action,
                               a.decision, a.reason, a.resource_id, a.details, a.user_id,
                               actor.email AS actor_email, r.title AS resource_title,
                               r.department AS resource_department
                          FROM audit_events a
                          LEFT JOIN users actor ON actor.tenant_id = a.tenant_id
                                               AND actor.id = a.user_id
                          LEFT JOIN resources r
                                 ON r.tenant_id = a.tenant_id AND r.id = a.resource_id
                         WHERE """  # noqa: S608 - clauses are constant fragments
                        + " AND ".join(clauses)
                        + " ORDER BY a.occurred_at DESC, a.id DESC LIMIT :limit"
                    ),
                    params,
                ).all()
        except Exception as exc:
            if before:
                raise AdminInvalid("invalid_cursor", "Invalid 'before' timestamp.") from exc
            raise
        page = rows[:limit]
        return {
            "events": [
                {
                    "event_id": str(r.id),
                    "occurred_at": _iso(r.occurred_at),
                    "request_id": r.request_id,
                    "event_type": r.event_type,
                    "action": r.action,
                    "decision": r.decision,
                    "reason": r.reason,
                    "actor": r.actor_email,
                    "resource_id": str(r.resource_id) if r.resource_id else None,
                    "resource_title": r.resource_title,
                    "resource_department": r.resource_department,
                    "details": json.loads(json.dumps(r.details or {})),
                }
                for r in page
            ],
            "next_before": _iso(page[-1].occurred_at) if len(rows) > limit and page else None,
        }
