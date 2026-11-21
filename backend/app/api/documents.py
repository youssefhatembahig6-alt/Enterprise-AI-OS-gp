"""Document routes (M3): upload, status, permissions, versions, reindex, delete, and
opening a cited passage. Unknown, foreign and unreadable documents and citations all
return the same 404."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.responses import Response

from app.api.admin import capability_disabled
from app.api.deps import CurrentContext
from app.api.errors import ApiError
from app.documents.ingest import (
    DocumentForbidden,
    DocumentNotFound,
    DocumentService,
    InvalidRequest,
    PermissionSpec,
)
from app.reporting.service import ReportService, RunNotFound
from app.schemas.auth import ErrorResponse
from app.schemas.documents import CitationOut, PermissionsIn, UploadIn, UploadOut, VersionIn
from app.tenancy.config import CapabilityDisabled

router = APIRouter(prefix="/api/v1", tags=["documents"])
_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def get_documents(request: Request) -> DocumentService:
    service: DocumentService = request.app.state.documents
    return service


def get_reports(request: Request) -> ReportService:
    service: ReportService = request.app.state.reports
    return service


Documents = Annotated[DocumentService, Depends(get_documents)]


def _decode(content: str) -> bytes:
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(422, "invalid_content", "content_base64 is not valid base64.") from exc


def _spec(p: PermissionsIn) -> PermissionSpec:
    grants = []
    for g in p.grants:
        if (g.group is None) == (g.user_email is None):
            raise ApiError(
                422, "invalid_grant", "Each grant needs exactly one group or user_email."
            )
        grants.append((g.effect, "group", g.group) if g.group else (g.effect, "user", g.user_email))
    countries = tuple(c.upper() if c != "*" else c for c in p.allowed_countries)
    return PermissionSpec(p.classification, p.department_restricted, countries, tuple(grants))  # type: ignore[arg-type]


def _errors(exc: Exception) -> ApiError:
    if isinstance(exc, CapabilityDisabled):
        return capability_disabled(exc)
    if isinstance(exc, DocumentNotFound):
        return ApiError(404, "not_found", "Not found.")
    if isinstance(exc, DocumentForbidden):
        return ApiError(403, "forbidden", "You cannot manage documents for this department.")
    if isinstance(exc, InvalidRequest):
        return ApiError(422, exc.code, exc.message)
    raise exc


@router.post(
    "/documents", response_model=UploadOut, status_code=status.HTTP_202_ACCEPTED, responses=_ERRORS
)
def upload(body: UploadIn, ctx: CurrentContext, docs: Documents) -> UploadOut:
    """Save the file and its permissions, then queue ingestion. Requires
    ``resource.admin`` for the department."""

    data = _decode(body.content_base64)
    try:
        document_id = docs.upload(
            ctx,
            filename=body.filename,
            data=data,
            department=body.department,
            permissions=_spec(body.permissions) if body.permissions else None,
            title=body.title,
        )
    except (DocumentNotFound, DocumentForbidden, InvalidRequest, CapabilityDisabled) as exc:
        raise _errors(exc) from exc
    return UploadOut(document_id=str(document_id), status="queued")


@router.get("/documents", responses=_ERRORS)
def list_documents(ctx: CurrentContext, docs: Documents) -> list[dict[str, Any]]:
    """Searchable documents the caller may read, plus every document the caller
    administers (with ingestion status; metadata only)."""

    return docs.visible(ctx)


@router.get("/documents/{document_id}", responses=_ERRORS)
def get_document(document_id: UUID, ctx: CurrentContext, docs: Documents) -> dict[str, Any]:
    try:
        return docs.get(ctx, document_id)
    except DocumentNotFound as exc:
        raise _errors(exc) from exc


@router.put(
    "/documents/{document_id}/permissions",
    status_code=204,
    response_class=Response,
    responses=_ERRORS,
)
def set_permissions(
    document_id: UUID, body: PermissionsIn, ctx: CurrentContext, docs: Documents
) -> Response:
    try:
        docs.set_permissions(ctx, document_id, _spec(body))
    except (DocumentNotFound, DocumentForbidden, InvalidRequest, CapabilityDisabled) as exc:
        raise _errors(exc) from exc
    return Response(status_code=204)


@router.post("/documents/{document_id}/versions", status_code=202, responses=_ERRORS)
def new_version(
    document_id: UUID, body: VersionIn, ctx: CurrentContext, docs: Documents
) -> dict[str, Any]:
    try:
        version = docs.new_version(
            ctx, document_id, filename=body.filename, data=_decode(body.content_base64)
        )
    except (DocumentNotFound, DocumentForbidden, InvalidRequest, CapabilityDisabled) as exc:
        raise _errors(exc) from exc
    return {"document_id": str(document_id), "version": version, "status": "queued"}


@router.post("/documents/{document_id}/reindex", status_code=202, responses=_ERRORS)
def reindex(document_id: UUID, ctx: CurrentContext, docs: Documents) -> dict[str, Any]:
    try:
        docs.reindex(ctx, document_id)
    except (DocumentNotFound, DocumentForbidden) as exc:
        raise _errors(exc) from exc
    return {"document_id": str(document_id), "status": "queued"}


@router.delete(
    "/documents/{document_id}", status_code=204, response_class=Response, responses=_ERRORS
)
def delete_document(document_id: UUID, ctx: CurrentContext, docs: Documents) -> Response:
    try:
        docs.delete(ctx, document_id)
    except (DocumentNotFound, DocumentForbidden, InvalidRequest) as exc:
        raise _errors(exc) from exc
    return Response(status_code=204)


@router.get("/runs/{run_id}/citations/{citation_id}", response_model=CitationOut, responses=_ERRORS)
def open_citation(
    run_id: UUID,
    citation_id: Annotated[str, Path(pattern=r"^c[0-9]{1,3}$")],
    ctx: CurrentContext,
    reports: Annotated[ReportService, Depends(get_reports)],
) -> CitationOut:
    """The exact cited passage of the version the answer used, sliced by Unicode code
    points on the server. Owner only; access is re-checked on every open."""

    try:
        return CitationOut.model_validate(reports.citation(ctx, run_id, citation_id))
    except RunNotFound as exc:
        raise ApiError(404, "not_found", "Not found.") from exc
