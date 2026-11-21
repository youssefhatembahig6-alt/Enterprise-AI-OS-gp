"""Tenant configuration: enabled packs, enabled connectors and the sync schedule
(ADR 0403, FR-12).

Packs and connectors are code; ``tenant_settings`` only switches them on per tenant.
Every backend entry point that uses a pack or connector calls ``require_pack`` /
``require_connector`` against the *current* row (never a cached copy, never a value
from the browser). A tenant without a row has nothing enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

# Pack -> what it switches on. Run kinds map to the pack that owns them.
PACKS: dict[str, str] = {
    "documents": "Document answers with citations (M3)",
    "sales_reports": "Verified sales reports over SQL (M2)",
}
CONNECTORS: dict[str, str] = {
    "upload": "Manual upload with explicit sharing",
    "google_drive": "Google Drive folder sync",
}
SYNC_MODES: tuple[str, ...] = ("demo", "nightly")
RUN_KIND_PACK: dict[str, str] = {"report": "sales_reports", "documents": "documents"}


class CapabilityDisabled(Exception):
    """The tenant has not enabled the pack or connector this request needs."""

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(f"{kind}:{name}")
        self.kind = kind
        self.name = name


@dataclass(frozen=True, slots=True)
class TenantSettings:
    enabled_packs: frozenset[str]
    enabled_connectors: frozenset[str]
    sync_mode: str
    sync_interval_seconds: int
    max_staleness_seconds: int
    version: int
    updated_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled_packs": sorted(self.enabled_packs),
            "enabled_connectors": sorted(self.enabled_connectors),
            "sync_mode": self.sync_mode,
            "sync_interval_seconds": self.sync_interval_seconds,
            "max_staleness_seconds": self.max_staleness_seconds,
            "version": self.version,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "available_packs": PACKS,
            "available_connectors": CONNECTORS,
        }


NOTHING_ENABLED = TenantSettings(frozenset(), frozenset(), "nightly", 86400, 108000, 0, None)


def load_settings(conn: Connection) -> TenantSettings:
    """Current settings of the caller's tenant (row-level security scopes the row)."""

    row = conn.execute(
        text(
            "SELECT enabled_packs, enabled_connectors, sync_mode, sync_interval_seconds,"
            " max_staleness_seconds, version, updated_at FROM tenant_settings"
        )
    ).one_or_none()
    if row is None:
        return NOTHING_ENABLED
    return TenantSettings(
        enabled_packs=frozenset(row.enabled_packs),
        enabled_connectors=frozenset(row.enabled_connectors),
        sync_mode=row.sync_mode,
        sync_interval_seconds=int(row.sync_interval_seconds),
        max_staleness_seconds=int(row.max_staleness_seconds),
        version=int(row.version),
        updated_at=row.updated_at,
    )


def pack_enabled(conn: Connection, run_kind: str) -> bool:
    """Whether the pack owning ``run_kind`` is enabled right now (runner checks)."""

    pack = RUN_KIND_PACK.get(run_kind)
    return pack is not None and pack in load_settings(conn).enabled_packs


def require_pack(conn: Connection, pack: str) -> TenantSettings:
    settings = load_settings(conn)
    if pack not in settings.enabled_packs:
        raise CapabilityDisabled("pack", pack)
    return settings


def require_connector(conn: Connection, connector: str) -> TenantSettings:
    settings = load_settings(conn)
    if connector not in settings.enabled_connectors:
        raise CapabilityDisabled("connector", connector)
    return settings


def update_settings(
    conn: Connection,
    *,
    tenant_id: Any,
    packs: list[str] | None,
    connectors: list[str] | None,
    sync_mode: str | None,
    updated_by: Any,
) -> TenantSettings:
    """Validate against the code vocabularies and store a new settings version."""

    current = load_settings(conn)
    new_packs = sorted(set(packs)) if packs is not None else sorted(current.enabled_packs)
    new_conns = (
        sorted(set(connectors)) if connectors is not None else sorted(current.enabled_connectors)
    )
    mode = sync_mode or current.sync_mode
    if unknown := [p for p in new_packs if p not in PACKS]:
        raise ValueError(f"unknown pack: {', '.join(unknown)}")
    if unknown := [c for c in new_conns if c not in CONNECTORS]:
        raise ValueError(f"unknown connector: {', '.join(unknown)}")
    if mode not in SYNC_MODES:
        raise ValueError(f"unknown sync mode: {mode}")
    conn.execute(
        text(
            """
            INSERT INTO tenant_settings (tenant_id, enabled_packs, enabled_connectors, sync_mode,
                                         version, updated_by, updated_at)
            VALUES (:t, :packs, :conns, :mode, 1, :u, now())
            ON CONFLICT (tenant_id) DO UPDATE SET
                enabled_packs = EXCLUDED.enabled_packs,
                enabled_connectors = EXCLUDED.enabled_connectors,
                sync_mode = EXCLUDED.sync_mode,
                version = tenant_settings.version + 1,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """
        ),
        {"t": tenant_id, "packs": new_packs, "conns": new_conns, "mode": mode, "u": updated_by},
    )
    return load_settings(conn)
