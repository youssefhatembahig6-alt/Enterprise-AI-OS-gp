"""Admin console API schemas (M4)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.auth import StrictModel

DEPARTMENT = r"^[a-z][a-z0-9_-]{0,62}$"
CLASSIFICATION = Literal["public", "internal", "confidential", "restricted"]


class TenantSettingsIn(StrictModel):
    enabled_packs: list[str] | None = Field(default=None, max_length=20)
    enabled_connectors: list[str] | None = Field(default=None, max_length=20)
    sync_mode: Literal["demo", "nightly"] | None = None


class UserPatchIn(StrictModel):
    status: Literal["active", "inactive"] | None = None
    clearance: CLASSIFICATION | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    department: str | None = Field(default=None, pattern=DEPARTMENT)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)


class RoleAssignmentIn(StrictModel):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    scope_department: str | None = Field(default=None, pattern=DEPARTMENT)


class UserRolesIn(StrictModel):
    assignments: list[RoleAssignmentIn] = Field(max_length=50)


class UserGroupsIn(StrictModel):
    groups: list[str] = Field(max_length=100)


class RoleIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(max_length=20)


class GroupLinksIn(StrictModel):
    google_groups: list[str] = Field(max_length=20)


class SourceIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    external_root: str = Field(pattern=r"^[A-Za-z0-9_-]{5,200}$", description="Drive folder id")
    department: str = Field(pattern=DEPARTMENT)
    classification: CLASSIFICATION
    department_restricted: bool = False
    allowed_countries: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)


class SourcePatchIn(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    department: str | None = Field(default=None, pattern=DEPARTMENT)
    classification: CLASSIFICATION | None = None
    department_restricted: bool | None = None
    allowed_countries: list[str] | None = Field(default=None, min_length=1, max_length=50)
    status: Literal["active", "paused", "deleted"] | None = None
