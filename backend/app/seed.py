"""Explicit, idempotent development seeding (runs with the migration/owner role).

Never runs implicitly: it is invoked only through ``python -m app.cli seed-dev``
and refuses unless EAIOS_ENV is ``development`` (or ``test`` from the test suite).
Re-running inserts nothing new for existing fixture entities.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, Engine, text

from app.fixtures import Foundation, TenantFx
from app.security.passwords import hash_password


class SeedRefused(RuntimeError):
    pass


@dataclass
class SeedResult:
    created: dict[str, int] = field(default_factory=dict)
    # tenant/email -> plaintext password, only for users created (or reset) in this run
    new_credentials: dict[str, dict[str, str]] = field(default_factory=dict)

    def bump(self, kind: str, inserted: int) -> None:
        self.created[kind] = self.created.get(kind, 0) + inserted


def _insert(conn: Connection, sql: str, params: dict[str, object]) -> int:
    return conn.execute(text(sql + " ON CONFLICT DO NOTHING"), params).rowcount


def _seed_tenant(
    conn: Connection,
    tenant: TenantFx,
    result: SeedResult,
    password_factory: Callable[[], str],
    reset_passwords: bool,
) -> None:
    result.bump(
        "tenants",
        _insert(
            conn,
            "INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)",
            {"id": tenant.id, "slug": tenant.slug, "name": tenant.name},
        ),
    )
    for role in tenant.roles.values():
        result.bump(
            "roles",
            _insert(
                conn,
                "INSERT INTO roles (id, tenant_id, key, name) VALUES (:id, :t, :key, :name)",
                {"id": role.id, "t": tenant.id, "key": role.key, "name": role.name},
            ),
        )
        for cap in role.capabilities:
            result.bump(
                "role_capabilities",
                _insert(
                    conn,
                    "INSERT INTO role_capabilities (tenant_id, role_id, capability)"
                    " VALUES (:t, :r, :c)",
                    {"t": tenant.id, "r": role.id, "c": cap},
                ),
            )
    for group in tenant.groups.values():
        result.bump(
            "groups",
            _insert(
                conn,
                "INSERT INTO groups (id, tenant_id, key, name) VALUES (:id, :t, :key, :name)",
                {"id": group.id, "t": tenant.id, "key": group.key, "name": group.name},
            ),
        )
    for user in tenant.users.values():
        password = password_factory()
        inserted = _insert(
            conn,
            "INSERT INTO users (id, tenant_id, email, display_name, password_hash, status,"
            " clearance, country, department)"
            " VALUES (:id, :t, :email, :name, :hash, :status, :clearance, :country, :dept)",
            {
                "id": user.id,
                "t": tenant.id,
                "email": user.email,
                "name": user.display_name,
                "hash": hash_password(password),
                "status": user.status,
                "clearance": user.clearance,
                "country": user.country,
                "dept": user.department,
            },
        )
        if not inserted and reset_passwords:
            conn.execute(
                text("UPDATE users SET password_hash = :hash WHERE id = :id"),
                {"hash": hash_password(password), "id": user.id},
            )
        if inserted or reset_passwords:
            result.new_credentials.setdefault(tenant.slug, {})[user.email] = password
        result.bump("users", inserted)
        for ur in user.roles:
            result.bump(
                "user_roles",
                _insert(
                    conn,
                    "INSERT INTO user_roles (tenant_id, user_id, role_id, scope_department)"
                    " VALUES (:t, :u, :r, :scope)",
                    {
                        "t": tenant.id,
                        "u": user.id,
                        "r": tenant.roles[ur.role].id,
                        "scope": ur.scope_department,
                    },
                ),
            )
        for gk in user.groups:
            result.bump(
                "group_memberships",
                _insert(
                    conn,
                    "INSERT INTO group_memberships (tenant_id, group_id, user_id)"
                    " VALUES (:t, :g, :u)",
                    {"t": tenant.id, "g": tenant.groups[gk].id, "u": user.id},
                ),
            )
    for res in tenant.resources.values():
        result.bump(
            "resources",
            _insert(
                conn,
                "INSERT INTO resources (id, tenant_id, kind, external_ref, title, classification,"
                " department, department_restricted, allowed_countries)"
                " VALUES (:id, :t, :kind, :ref, :title, :cls, :dept, :restricted, :countries)",
                {
                    "id": res.id,
                    "t": tenant.id,
                    "kind": res.kind,
                    "ref": f"fixture:{res.key}",
                    "title": res.title,
                    "cls": res.classification,
                    "dept": res.department,
                    "restricted": res.department_restricted,
                    "countries": list(res.allowed_countries),
                },
            ),
        )
        for grant in res.grants:
            result.bump(
                "resource_grants",
                _insert(
                    conn,
                    "INSERT INTO resource_grants (id, tenant_id, resource_id, user_id, group_id,"
                    " effect, action) VALUES (:id, :t, :r, :u, :g, :effect, :action)",
                    {
                        "id": grant.id,
                        "t": tenant.id,
                        "r": res.id,
                        "u": tenant.users[grant.user].id if grant.user else None,
                        "g": tenant.groups[grant.group].id if grant.group else None,
                        "effect": grant.effect,
                        "action": grant.action,
                    },
                ),
            )
    if tenant.settings is not None:
        # Upsert: migration 0007 gives existing tenants a default row; the fixture wins.
        result.bump(
            "tenant_settings",
            conn.execute(
                text(
                    """
                    INSERT INTO tenant_settings (tenant_id, enabled_packs, enabled_connectors,
                                                 sync_mode)
                    VALUES (:t, :packs, :conns, :mode)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        enabled_packs = EXCLUDED.enabled_packs,
                        enabled_connectors = EXCLUDED.enabled_connectors,
                        sync_mode = EXCLUDED.sync_mode
                    WHERE (tenant_settings.enabled_packs, tenant_settings.enabled_connectors,
                           tenant_settings.sync_mode)
                          IS DISTINCT FROM (EXCLUDED.enabled_packs,
                                            EXCLUDED.enabled_connectors, EXCLUDED.sync_mode)
                    """
                ),
                {
                    "t": tenant.id,
                    "packs": tenant.settings["enabled_packs"],
                    "conns": tenant.settings["enabled_connectors"],
                    "mode": tenant.settings["sync_mode"],
                },
            ).rowcount,
        )
    for profile in tenant.report_profiles.values():
        result.bump(
            "tenant_report_profiles",
            _insert(
                conn,
                "INSERT INTO tenant_report_profiles (tenant_id, report_key, resource_id, version,"
                " config) VALUES (:t, :key, :r, :version, CAST(:config AS jsonb))",
                {
                    "t": tenant.id,
                    "key": profile.key,
                    "r": tenant.resources[profile.resource].id,
                    "version": profile.version,
                    "config": json.dumps(profile.config, sort_keys=True),
                },
            ),
        )


def seed_foundation(
    owner_engine: Engine,
    foundation: Foundation,
    *,
    environment: str,
    password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    reset_passwords: bool = False,
) -> SeedResult:
    if environment not in ("development", "test"):
        raise SeedRefused(f"seeding is development-only; EAIOS_ENV={environment!r}")
    result = SeedResult()
    with owner_engine.begin() as conn:
        for tenant in foundation.tenants.values():
            _seed_tenant(conn, tenant, result, password_factory, reset_passwords)
    return result


def write_credentials(path: Path, new_credentials: dict[str, dict[str, str]]) -> int:
    """Merge newly created passwords into an owner-only JSON file. Returns count written.

    * An empty change set never touches the file (a no-op reseed preserves it).
    * Changes are written to an owner-only temporary file in the same directory,
      flushed and fsynced, then atomically swapped in with ``os.replace``. An
      interruption leaves the previous valid file untouched.
    * An existing file that cannot be parsed is never overwritten.

    The database transaction commits before this runs. If this step fails, the
    database holds hashes for passwords that were never saved; recover with
    ``seed-dev --reset-passwords`` (see docs/runbook.md).
    """

    count = sum(len(users) for users in new_credentials.values())
    if count == 0:
        return 0
    existing: dict[str, dict[str, str]] = {}
    if path.exists():
        existing = json.loads(path.read_text())
    for tenant, users in new_credentials.items():
        existing.setdefault(tenant, {}).update(users)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(existing, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)
    return count


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
