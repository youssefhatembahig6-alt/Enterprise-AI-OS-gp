"""Reference authorization evaluator (pure, no I/O).

This module defines what "allowed" means. Every later access path (SQL,
retrieval, downloads, caching) must agree with it. See
``docs/adr/0103-authorization-reference-policy.md`` for the truth table.

Content read (``resource.read``) requires all five layers:

1. Tenant         - subject and resource belong to the same tenant
2. Role           - the subject holds the ``resource.read`` capability through a role
                    assignment whose scope covers the resource's department
3. Attribute      - department and country rules on the resource are satisfied
4. Resource grant - an *allow* grant names the user directly or one of the user's groups
5. Classification - the user's clearance ranks at or above the resource classification

An explicit *deny* grant for the user or any of their groups overrides any allow.
Missing (unknown) required inputs deny. A direct user grant satisfies layer 4 only.

Querying a protected dataset (``dataset.query``, M2 reports) uses the same five
layers with its own capability and its own grant action, so holding
``resource.read`` never implies report access and vice versa.

Metadata administration (``resource.admin``) requires tenant plus the
``resource.admin`` capability with a covering scope. It never implies content read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class Action(StrEnum):
    RESOURCE_READ = "resource.read"
    RESOURCE_ADMIN = "resource.admin"
    DATASET_QUERY = "dataset.query"


class Capability(StrEnum):
    """Every capability a role may carry. Fixtures may only use these."""

    RESOURCE_READ = "resource.read"
    RESOURCE_ADMIN = "resource.admin"
    DATASET_QUERY = "dataset.query"
    # M4 scoped administration (never imply content read).
    IDENTITY_ADMIN = "identity.admin"  # users, role assignments, group memberships
    AUDIT_READ = "audit.read"  # audit activity
    TENANT_CONFIG = "tenant.config"  # packs and connectors (tenant-wide only)


# Capability required for each action.
ACTION_CAPABILITY: dict[Action, Capability] = {
    Action.RESOURCE_READ: Capability.RESOURCE_READ,
    Action.RESOURCE_ADMIN: Capability.RESOURCE_ADMIN,
    Action.DATASET_QUERY: Capability.DATASET_QUERY,
}

# Content actions: all five layers apply, with a grant for that same action.
CONTENT_ACTIONS: frozenset[Action] = frozenset({Action.RESOURCE_READ, Action.DATASET_QUERY})

# Classification order, lowest to highest. Clearance uses the same scale.
CLASSIFICATIONS: tuple[str, ...] = ("public", "internal", "confidential", "restricted")
CLASSIFICATION_RANK: dict[str, int] = {name: rank for rank, name in enumerate(CLASSIFICATIONS)}

ANY_COUNTRY = "*"


class Reason(StrEnum):
    ALLOW = "allow"
    UNKNOWN_ACTION = "deny.unknown_action"
    INACTIVE_SUBJECT = "deny.inactive_subject"
    TENANT_MISMATCH = "deny.tenant_mismatch"
    RESOURCE_INACTIVE = "deny.resource_inactive"
    EXPLICIT_DENY = "deny.explicit_deny"
    MISSING_CAPABILITY = "deny.missing_capability"
    CAPABILITY_OUT_OF_SCOPE = "deny.capability_out_of_scope"
    DEPARTMENT_ATTRIBUTE = "deny.attribute_department"
    COUNTRY_ATTRIBUTE = "deny.attribute_country"
    NO_GRANT = "deny.no_grant"
    CLEARANCE = "deny.clearance"
    UNKNOWN_INPUT = "deny.unknown_input"
    NOT_FOUND = "deny.not_found"


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """A capability obtained through a role assignment.

    ``scope_department`` of ``None`` means tenant-wide; otherwise the capability
    only applies to resources whose owning department equals the scope.
    """

    capability: str
    scope_department: str | None = None


@dataclass(frozen=True, slots=True)
class Subject:
    """Trusted, server-built facts about the requesting user."""

    tenant_id: UUID
    user_id: UUID
    active: bool
    capabilities: frozenset[CapabilityGrant] = frozenset()
    group_ids: frozenset[UUID] = frozenset()
    clearance: str | None = None
    country: str | None = None
    department: str | None = None


@dataclass(frozen=True, slots=True)
class Grant:
    effect: str  # "allow" | "deny"
    action: str
    user_id: UUID | None = None
    group_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ResourceMeta:
    tenant_id: UUID
    resource_id: UUID
    classification: str | None
    department: str | None
    department_restricted: bool | None
    allowed_countries: tuple[str, ...] | None
    active: bool = True
    grants: tuple[Grant, ...] = field(default_factory=tuple)
    # M3: permissions saved but not yet confirmed (for example an upload whose sharing
    # was not specified). Unknown permissions deny every content action (T7).
    permissions_verified: bool = True


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: Reason

    @classmethod
    def allow(cls) -> Decision:
        return cls(True, Reason.ALLOW)

    @classmethod
    def deny(cls, reason: Reason) -> Decision:
        return cls(False, reason)


def _matches_subject(grant: Grant, subject: Subject) -> bool:
    if grant.user_id is not None:
        return grant.user_id == subject.user_id
    if grant.group_id is not None:
        return grant.group_id in subject.group_ids
    return False


def _capability_check(
    subject: Subject, capability: Capability, department: str | None
) -> Reason | None:
    held = [c for c in subject.capabilities if c.capability == capability.value]
    if not held:
        return Reason.MISSING_CAPABILITY
    if any(c.scope_department is None for c in held):
        return None
    if department is None:
        return Reason.UNKNOWN_INPUT
    if any(c.scope_department == department for c in held):
        return None
    return Reason.CAPABILITY_OUT_OF_SCOPE


def _attribute_check(subject: Subject, resource: ResourceMeta) -> Reason | None:
    if resource.department_restricted is None or resource.allowed_countries is None:
        return Reason.UNKNOWN_INPUT
    if resource.department_restricted:
        if resource.department is None or subject.department is None:
            return Reason.UNKNOWN_INPUT
        if subject.department != resource.department:
            return Reason.DEPARTMENT_ATTRIBUTE
    countries = resource.allowed_countries
    if not countries:
        return Reason.COUNTRY_ATTRIBUTE
    if ANY_COUNTRY not in countries:
        if subject.country is None:
            return Reason.UNKNOWN_INPUT
        if subject.country not in countries:
            return Reason.COUNTRY_ATTRIBUTE
    return None


def _grant_checks(
    subject: Subject, grants: Iterable[Grant], action: Action
) -> tuple[bool, bool, bool]:
    """Return (explicit_deny, has_allow, malformed)."""

    explicit_deny = has_allow = malformed = False
    for grant in grants:
        if grant.action != action.value:
            continue
        if grant.effect not in ("allow", "deny") or (grant.user_id is None) == (
            grant.group_id is None
        ):
            malformed = True
            continue
        if not _matches_subject(grant, subject):
            continue
        if grant.effect == "deny":
            explicit_deny = True
        else:
            has_allow = True
    return explicit_deny, has_allow, malformed


def capability_scope(subject: Subject, capability: Capability) -> frozenset[str] | None:
    """Departments covered by ``capability``: ``None`` means tenant-wide, an empty set
    means the capability is not held. Inactive subjects hold nothing."""

    if not subject.active:
        return frozenset()
    held = [c for c in subject.capabilities if c.capability == capability.value]
    if any(c.scope_department is None for c in held):
        return None
    return frozenset(c.scope_department for c in held if c.scope_department is not None)


def covers(subject: Subject, capability: Capability, department: str | None) -> bool:
    """Whether ``capability`` applies to ``department`` (``None``: tenant-wide only)."""

    scope = capability_scope(subject, capability)
    if scope is None:
        return True
    return department is not None and department in scope


def evaluate(subject: Subject, action: str, resource: ResourceMeta) -> Decision:
    """Evaluate one action on one resource. Fails closed on anything unexpected."""

    try:
        act = Action(action)
    except ValueError:
        return Decision.deny(Reason.UNKNOWN_ACTION)

    if not subject.active:
        return Decision.deny(Reason.INACTIVE_SUBJECT)
    # Layer 1: tenant. Public classification never crosses this boundary.
    if subject.tenant_id != resource.tenant_id:
        return Decision.deny(Reason.TENANT_MISMATCH)
    if not resource.active:
        return Decision.deny(Reason.RESOURCE_INACTIVE)

    if act is Action.RESOURCE_ADMIN:
        failure = _capability_check(subject, Capability.RESOURCE_ADMIN, resource.department)
        return Decision.deny(failure) if failure else Decision.allow()

    if not resource.permissions_verified:
        return Decision.deny(Reason.UNKNOWN_INPUT)

    # Content actions — explicit deny is evaluated before any allow can apply.
    explicit_deny, has_allow, malformed = _grant_checks(subject, resource.grants, act)
    if explicit_deny:
        return Decision.deny(Reason.EXPLICIT_DENY)
    if malformed:
        return Decision.deny(Reason.UNKNOWN_INPUT)

    # Layer 2: role capability (with department scope).
    failure = _capability_check(subject, ACTION_CAPABILITY[act], resource.department)
    if failure:
        return Decision.deny(failure)

    # Layer 3: attributes.
    failure = _attribute_check(subject, resource)
    if failure:
        return Decision.deny(failure)

    # Layer 4: resource grant (direct user or group).
    if not has_allow:
        return Decision.deny(Reason.NO_GRANT)

    # Layer 5: classification vs clearance.
    if resource.classification is None or subject.clearance is None:
        return Decision.deny(Reason.UNKNOWN_INPUT)
    resource_rank = CLASSIFICATION_RANK.get(resource.classification)
    clearance_rank = CLASSIFICATION_RANK.get(subject.clearance)
    if resource_rank is None or clearance_rank is None:
        return Decision.deny(Reason.UNKNOWN_INPUT)
    if clearance_rank < resource_rank:
        return Decision.deny(Reason.CLEARANCE)

    return Decision.allow()
