"""Admin console routes (M4) and the tenant feature list.

Missing admin capability → 403 ``forbidden``; an object outside the caller's scope →
404 ``not_found``; a pack or connector the tenant has not enabled → 403
``capability_disabled``. All checks are in ``app.admin.service``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.admin.service import AdminForbidden, AdminInvalid, AdminNotFound, AdminService
from app.api.deps import CurrentContext
from app.api.errors import ApiError
from app.schemas.admin import (
    GroupLinksIn,
    RoleIn,
    SourceIn,
    SourcePatchIn,
    TenantSettingsIn,
    UserGroupsIn,
    UserPatchIn,
    UserRolesIn,
)
from app.schemas.auth import ErrorResponse
from app.tenancy.config import CapabilityDisabled

router = APIRouter(prefix="/api/v1", tags=["admin"])
_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def get_admin(request: Request) -> AdminService:
    service: AdminService = request.app.state.admin
    return service


Admin = Annotated[AdminService, Depends(get_admin)]


def capability_disabled(exc: CapabilityDisabled) -> ApiError:
    return ApiError(
        403,
        "capability_disabled",
        f"The {exc.name.replace('_', ' ')} {exc.kind} is not enabled for your organization.",
    )


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except AdminForbidden as exc:
        raise ApiError(403, "forbidden", "You are not allowed to do this.") from exc
    except AdminNotFound as exc:
        raise ApiError(404, "not_found", "Not found.") from exc
    except AdminInvalid as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except CapabilityDisabled as exc:
        raise capability_disabled(exc) from exc


@router.get("/tenant/settings", responses=_ERRORS)
def tenant_settings(ctx: CurrentContext, admin: Admin) -> dict[str, Any]:
    """Enabled packs and connectors of the caller's organization (any signed-in user)."""

    return _call(lambda: admin.tenant_settings(ctx))


@router.put("/admin/tenant/settings", responses=_ERRORS)
def update_tenant_settings(
    ctx: CurrentContext, admin: Admin, body: TenantSettingsIn
) -> dict[str, Any]:
    """Switch packs/connectors and the sync schedule. Requires tenant-wide ``tenant.config``."""

    return _call(
        lambda: admin.update_tenant_settings(
            ctx, body.enabled_packs, body.enabled_connectors, body.sync_mode
        )
    )


@router.get("/admin/users", responses=_ERRORS)
def list_users(ctx: CurrentContext, admin: Admin) -> list[dict[str, Any]]:
    return _call(lambda: admin.list_users(ctx))


@router.patch("/admin/users/{user_id}", responses=_ERRORS)
def update_user(
    user_id: UUID, body: UserPatchIn, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    return _call(lambda: admin.update_user(ctx, user_id, body.model_dump(exclude_unset=True)))


@router.put("/admin/users/{user_id}/roles", responses=_ERRORS)
def set_user_roles(
    user_id: UUID, body: UserRolesIn, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    assignments = [(a.role, a.scope_department) for a in body.assignments]
    return _call(lambda: admin.set_user_roles(ctx, user_id, assignments))


@router.put("/admin/users/{user_id}/groups", responses=_ERRORS)
def set_user_groups(
    user_id: UUID, body: UserGroupsIn, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    return _call(lambda: admin.set_user_groups(ctx, user_id, body.groups))


@router.get("/admin/roles", responses=_ERRORS)
def list_roles(ctx: CurrentContext, admin: Admin) -> list[dict[str, Any]]:
    return _call(lambda: admin.list_roles(ctx))


@router.put("/admin/roles/{role_key}", responses=_ERRORS)
def upsert_role(role_key: str, body: RoleIn, ctx: CurrentContext, admin: Admin) -> dict[str, Any]:
    return _call(lambda: admin.upsert_role(ctx, role_key, body.name, body.capabilities))


@router.get("/admin/groups", responses=_ERRORS)
def list_groups(ctx: CurrentContext, admin: Admin) -> list[dict[str, Any]]:
    return _call(lambda: admin.list_groups(ctx))


@router.put("/admin/groups/{group_key}/links", responses=_ERRORS)
def set_group_links(
    group_key: str, body: GroupLinksIn, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    """Link Google group emails to an internal group (Drive group permissions)."""

    return _call(lambda: admin.set_group_links(ctx, group_key, body.google_groups))


@router.get("/admin/documents", responses=_ERRORS)
def list_documents(ctx: CurrentContext, admin: Admin) -> list[dict[str, Any]]:
    """Documents in the caller's ``resource.admin`` scope, with sharing (metadata only)."""

    return _call(lambda: admin.list_documents(ctx))


@router.get("/admin/sources", responses=_ERRORS)
def list_sources(ctx: CurrentContext, admin: Admin) -> list[dict[str, Any]]:
    return _call(lambda: admin.list_sources(ctx))


@router.post("/admin/sources", status_code=201, responses=_ERRORS)
def create_source(body: SourceIn, ctx: CurrentContext, admin: Admin) -> dict[str, Any]:
    payload = body.model_dump()
    payload["allowed_countries"] = [c.upper() if c != "*" else c for c in body.allowed_countries]
    return _call(lambda: admin.create_source(ctx, payload))


@router.get("/admin/sources/{source_id}", responses=_ERRORS)
def get_source(source_id: UUID, ctx: CurrentContext, admin: Admin) -> dict[str, Any]:
    return _call(lambda: admin.get_source(ctx, source_id))


@router.patch("/admin/sources/{source_id}", responses=_ERRORS)
def update_source(
    source_id: UUID, body: SourcePatchIn, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if changes.get("allowed_countries"):
        changes["allowed_countries"] = [
            c.upper() if c != "*" else c for c in changes["allowed_countries"]
        ]
    return _call(lambda: admin.update_source(ctx, source_id, changes))


@router.post("/admin/sources/{source_id}/sync", status_code=202, responses=_ERRORS)
def sync_source(source_id: UUID, ctx: CurrentContext, admin: Admin) -> dict[str, Any]:
    """Run a sync now (inline dispatch) or queue it (Celery)."""

    return _call(lambda: admin.sync_source(ctx, source_id))


@router.get("/admin/sources/{source_id}/runs/{run_id}", responses=_ERRORS)
def get_sync_run(
    source_id: UUID, run_id: UUID, ctx: CurrentContext, admin: Admin
) -> dict[str, Any]:
    """One sync run (for waiting on the run a sync request created)."""

    return _call(lambda: admin.get_run(ctx, source_id, run_id))


@router.get("/admin/audit", responses=_ERRORS)
def audit(
    ctx: CurrentContext,
    admin: Admin,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=40)] = None,
    event_type: Annotated[str | None, Query(max_length=80)] = None,
    resource_id: UUID | None = None,
) -> dict[str, Any]:
    """Audit activity in the caller's ``audit.read`` scope, newest first."""

    return _call(
        lambda: admin.audit(
            ctx, limit=limit, before=before, event_type=event_type, resource_id=resource_id
        )
    )
