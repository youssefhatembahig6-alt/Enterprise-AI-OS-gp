"""Declarative foundation fixtures: loading, validation and in-memory policy inputs.

Fixture entities get deterministic UUIDv5 identifiers, so the in-memory model and
the seeded database rows share IDs and seeding is idempotent.
"""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.security.policy import (
    ANY_COUNTRY,
    CLASSIFICATION_RANK,
    Action,
    Capability,
    CapabilityGrant,
    Grant,
    ResourceMeta,
    Subject,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FOUNDATION_FIXTURE = FIXTURE_DIR / "foundation.toml"
POLICY_CASES = FIXTURE_DIR / "policy_cases.toml"

_NAMESPACE = uuid.UUID("6f1d3c52-3b7e-4f0e-9a51-0d1c5e8b7a10")


def fixture_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, "/".join(parts))


class FixtureError(ValueError):
    pass


@dataclass(frozen=True)
class RoleFx:
    id: uuid.UUID
    key: str
    name: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class GroupFx:
    id: uuid.UUID
    key: str
    name: str


@dataclass(frozen=True)
class UserRoleFx:
    role: str
    scope_department: str | None


@dataclass(frozen=True)
class UserFx:
    id: uuid.UUID
    key: str
    email: str
    display_name: str
    status: str
    clearance: str | None
    country: str | None
    department: str | None
    roles: tuple[UserRoleFx, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True)
class GrantFx:
    id: uuid.UUID
    effect: str
    user: str | None
    group: str | None
    action: str = Action.RESOURCE_READ.value


@dataclass(frozen=True)
class ResourceFx:
    id: uuid.UUID
    key: str
    kind: str
    title: str
    classification: str
    department: str
    department_restricted: bool
    allowed_countries: tuple[str, ...]
    grants: tuple[GrantFx, ...]


@dataclass(frozen=True)
class ReportProfileFx:
    key: str
    resource: str
    version: int
    config: dict[str, Any]


@dataclass(frozen=True)
class TenantFx:
    id: uuid.UUID
    slug: str
    name: str
    roles: dict[str, RoleFx] = field(default_factory=dict)
    groups: dict[str, GroupFx] = field(default_factory=dict)
    users: dict[str, UserFx] = field(default_factory=dict)
    resources: dict[str, ResourceFx] = field(default_factory=dict)
    report_profiles: dict[str, ReportProfileFx] = field(default_factory=dict)
    # M4 tenant configuration (None: the tenant has nothing enabled).
    settings: dict[str, Any] | None = None


@dataclass(frozen=True)
class Foundation:
    tenants: dict[str, TenantFx]

    def tenant_user(self, ref: str) -> tuple[TenantFx, UserFx]:
        slug, key = ref.split("/", 1)
        tenant = self.tenants[slug]
        return tenant, tenant.users[key]

    def tenant_resource(self, ref: str) -> tuple[TenantFx, ResourceFx]:
        slug, key = ref.split("/", 1)
        tenant = self.tenants[slug]
        return tenant, tenant.resources[key]


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise FixtureError(message)


def _parse_tenant(raw: dict[str, Any]) -> TenantFx:
    slug = raw["slug"]
    tenant = TenantFx(id=fixture_id(slug, "tenant"), slug=slug, name=raw["name"])
    known_caps = {c.value for c in Capability}

    for r in raw.get("roles", []):
        caps = tuple(r["capabilities"])
        _require(set(caps) <= known_caps, f"{slug}: undefined capability in role {r['key']}")
        tenant.roles[r["key"]] = RoleFx(
            fixture_id(slug, "role", r["key"]), r["key"], r["name"], caps
        )
    for g in raw.get("groups", []):
        tenant.groups[g["key"]] = GroupFx(fixture_id(slug, "group", g["key"]), g["key"], g["name"])
    for u in raw.get("users", []):
        _require(u["status"] in ("active", "inactive"), f"{slug}: bad status for {u['key']}")
        clearance = u.get("clearance")
        _require(
            clearance is None or clearance in CLASSIFICATION_RANK,
            f"{slug}: undefined clearance for {u['key']}",
        )
        roles = tuple(UserRoleFx(x["role"], x.get("scope_department")) for x in u.get("roles", []))
        for ur in roles:
            _require(ur.role in tenant.roles, f"{slug}: user {u['key']} has unknown role {ur.role}")
        groups = tuple(u.get("groups", []))
        for gk in groups:
            _require(gk in tenant.groups, f"{slug}: user {u['key']} has unknown group {gk}")
        tenant.users[u["key"]] = UserFx(
            id=fixture_id(slug, "user", u["key"]),
            key=u["key"],
            email=u["email"].lower(),
            display_name=u["display_name"],
            status=u["status"],
            clearance=clearance,
            country=u.get("country"),
            department=u.get("department"),
            roles=roles,
            groups=groups,
        )
    for res in raw.get("resources", []):
        _require(
            res["classification"] in CLASSIFICATION_RANK,
            f"{slug}: undefined classification for {res['key']}",
        )
        grants = []
        for i, g in enumerate(res.get("grants", [])):
            _require(g["effect"] in ("allow", "deny"), f"{slug}: bad grant effect")
            user, group = g.get("user"), g.get("group")
            _require((user is None) != (group is None), f"{slug}: grant needs user xor group")
            _require(user is None or user in tenant.users, f"{slug}: grant to unknown user {user}")
            _require(group is None or group in tenant.groups, f"{slug}: unknown group {group}")
            action = g.get("action", Action.RESOURCE_READ.value)
            _require(
                action in (Action.RESOURCE_READ.value, Action.DATASET_QUERY.value),
                f"{slug}: grant action {action!r} is not a content action",
            )
            grants.append(
                GrantFx(
                    fixture_id(slug, "grant", res["key"], str(i)),
                    g["effect"],
                    user,
                    group,
                    action,
                )
            )
        countries = tuple(res["allowed_countries"])
        _require(len(countries) >= 1, f"{slug}: {res['key']} needs allowed_countries")
        kind = res.get("kind", "document")
        _require(kind in ("document", "dataset"), f"{slug}: bad resource kind {kind!r}")
        tenant.resources[res["key"]] = ResourceFx(
            id=fixture_id(slug, "resource", res["key"]),
            key=res["key"],
            kind=kind,
            title=res["title"],
            classification=res["classification"],
            department=res["department"],
            department_restricted=bool(res["department_restricted"]),
            allowed_countries=countries,
            grants=tuple(grants),
        )
    if (settings := raw.get("settings")) is not None:
        from app.tenancy.config import CONNECTORS, PACKS, SYNC_MODES  # noqa: PLC0415

        packs = list(settings.get("enabled_packs", []))
        connectors = list(settings.get("enabled_connectors", []))
        _require(set(packs) <= set(PACKS), f"{slug}: unknown pack in settings")
        _require(set(connectors) <= set(CONNECTORS), f"{slug}: unknown connector in settings")
        _require(settings.get("sync_mode", "nightly") in SYNC_MODES, f"{slug}: bad sync_mode")
        tenant = replace(
            tenant,
            settings={
                "enabled_packs": sorted(packs),
                "enabled_connectors": sorted(connectors),
                "sync_mode": settings.get("sync_mode", "nightly"),
            },
        )
    for prof in raw.get("report_profiles", []):
        _require(
            prof["resource"] in tenant.resources
            and tenant.resources[prof["resource"]].kind == "dataset",
            f"{slug}: report profile {prof['key']} needs a dataset resource",
        )
        tenant.report_profiles[prof["key"]] = ReportProfileFx(
            key=prof["key"],
            resource=prof["resource"],
            version=int(prof["version"]),
            config=dict(prof["config"]),
        )
    return tenant


def load_foundation(path: Path = FOUNDATION_FIXTURE) -> Foundation:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    _require(raw.get("version") == 1, "unsupported fixture version")
    tenants = {t["slug"]: _parse_tenant(t) for t in raw["tenants"]}
    return Foundation(tenants=tenants)


def load_policy_cases(path: Path = POLICY_CASES) -> list[dict[str, str]]:
    with path.open("rb") as handle:
        cases: list[dict[str, str]] = tomllib.load(handle)["cases"]
    for case in cases:
        Action(case["action"])
    return cases


def subject_for(tenant: TenantFx, user: UserFx) -> Subject:
    caps = frozenset(
        CapabilityGrant(capability=cap, scope_department=ur.scope_department)
        for ur in user.roles
        for cap in tenant.roles[ur.role].capabilities
    )
    return Subject(
        tenant_id=tenant.id,
        user_id=user.id,
        active=user.status == "active",
        capabilities=caps,
        group_ids=frozenset(tenant.groups[g].id for g in user.groups),
        clearance=user.clearance,
        country=user.country,
        department=user.department,
    )


def resource_for(tenant: TenantFx, res: ResourceFx) -> ResourceMeta:
    return ResourceMeta(
        tenant_id=tenant.id,
        resource_id=res.id,
        classification=res.classification,
        department=res.department,
        department_restricted=res.department_restricted,
        allowed_countries=res.allowed_countries,
        active=True,
        grants=tuple(
            Grant(
                effect=g.effect,
                action=g.action,
                user_id=tenant.users[g.user].id if g.user else None,
                group_id=tenant.groups[g.group].id if g.group else None,
            )
            for g in res.grants
        ),
    )


__all__ = ["ANY_COUNTRY", "Foundation", "fixture_id", "load_foundation", "load_policy_cases"]
