"""Capability scope helpers and pack-aware routing (M4)."""

from __future__ import annotations

import uuid

import pytest

from app.api.reports import choose_kind
from app.security.policy import Capability, CapabilityGrant, Subject, capability_scope, covers
from app.tenancy.config import CapabilityDisabled

T, U = uuid.uuid4(), uuid.uuid4()


def subject(*grants: tuple[str, str | None], active: bool = True) -> Subject:
    return Subject(
        tenant_id=T,
        user_id=U,
        active=active,
        capabilities=frozenset(CapabilityGrant(c, d) for c, d in grants),
    )


def test_scope_is_tenant_wide_departments_or_nothing() -> None:
    assert capability_scope(subject(("identity.admin", None)), Capability.IDENTITY_ADMIN) is None
    scoped = subject(("identity.admin", "finance"), ("identity.admin", "hr"))
    assert capability_scope(scoped, Capability.IDENTITY_ADMIN) == frozenset({"finance", "hr"})
    assert capability_scope(scoped, Capability.AUDIT_READ) == frozenset()
    inactive = subject(("identity.admin", None), active=False)
    assert capability_scope(inactive, Capability.IDENTITY_ADMIN) == frozenset()


def test_covers() -> None:
    scoped = subject(("resource.admin", "finance"))
    assert covers(scoped, Capability.RESOURCE_ADMIN, "finance")
    assert not covers(scoped, Capability.RESOURCE_ADMIN, "hr")
    assert not covers(scoped, Capability.RESOURCE_ADMIN, None)  # tenant-wide needs None scope
    assert covers(subject(("resource.admin", None)), Capability.RESOURCE_ADMIN, None)


def test_routing_respects_enabled_packs() -> None:
    sales = "What were fulfilled sales in Q1 2026?"
    both = frozenset({"documents", "sales_reports"})
    assert choose_kind(sales, "auto", both) == "report"
    assert choose_kind(sales, "auto", frozenset({"documents"})) == "documents"
    assert (
        choose_kind("What is the leave policy?", "auto", frozenset({"sales_reports"})) == "report"
    )
    with pytest.raises(CapabilityDisabled):
        choose_kind(sales, "report", frozenset({"documents"}))
    with pytest.raises(CapabilityDisabled):
        choose_kind(sales, "auto", frozenset())
