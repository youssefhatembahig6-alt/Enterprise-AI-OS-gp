"""Report routes: POST /chat streams a run over SSE; runs can be replayed, inspected
and cancelled; evidence is viewable while the caller is still authorized.

Foreign, unknown and no-longer-authorized runs and evidence all return the same 404.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.admin import capability_disabled
from app.api.deps import CurrentContext
from app.api.errors import ApiError, unauthenticated
from app.reporting.router import _TOPIC, _YEAR_ONLY, extract_periods
from app.reporting.service import ReportService, RunNotFound
from app.schemas.auth import ErrorResponse
from app.schemas.reports import ChatRequest, EvidenceOut, RunOut
from app.tenancy.config import RUN_KIND_PACK, CapabilityDisabled

router = APIRouter(prefix="/api/v1", tags=["reports"])
_bearer = HTTPBearer(auto_error=False)

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",  # disable proxy buffering
    "Connection": "keep-alive",
}
_SSE_DOC: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Server-Sent Events: status, token, citation, approval_required, "
        "error, done (docs/contracts/reports.md). Event ids increase per run.",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    },
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def choose_kind(question: str, mode: str, enabled: frozenset[str] | None = None) -> str:
    """``auto`` keeps M2 behaviour for anything that looks like a sales/period request
    (topic word, quarter, month or year) and sends the rest to document answers.

    M4: only packs the tenant enabled are routable. An explicit mode for a disabled
    pack raises ``CapabilityDisabled``; ``auto`` falls back to the enabled pack."""

    if mode == "report":
        kind = "report"
    elif mode == "documents":
        kind = "documents"
    elif _TOPIC.search(question) or extract_periods(question) or _YEAR_ONLY.search(question):
        kind = "report"
    else:
        kind = "documents"
    if enabled is None or RUN_KIND_PACK[kind] in enabled:
        return kind
    if mode == "auto":
        for other in ("documents", "report"):
            if RUN_KIND_PACK[other] in enabled:
                return other
    raise CapabilityDisabled("pack", RUN_KIND_PACK[kind])


def get_report_service(request: Request) -> ReportService:
    service: ReportService = request.app.state.reports
    return service


Service = Annotated[ReportService, Depends(get_report_service)]


def _token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthenticated()
    return credentials.credentials


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Not found.")


def _require_available(service: ReportService) -> None:
    if not service.available:
        raise ApiError(503, "reports_unavailable", "Reporting is not configured on this server.")


@router.post("/chat", response_class=StreamingResponse, responses=_SSE_DOC)
def chat(
    body: ChatRequest,
    ctx: CurrentContext,
    service: Service,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> StreamingResponse:
    """Create a report run and stream its events. Only status events are sent before
    the result passes verification."""

    _require_available(service)
    token = _token(credentials)
    try:
        run = service.create_run(
            ctx,
            body.question.strip(),
            body.client_request_id or secrets.token_urlsafe(16),
            choose_kind(body.question, body.mode, service.enabled_packs(ctx)),
        )
    except CapabilityDisabled as exc:
        raise capability_disabled(exc) from exc
    return StreamingResponse(
        service.stream(token, ctx, run.id, 0),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-ID": str(run.id)},
    )


@router.get("/runs/{run_id}/events", response_class=StreamingResponse, responses=_SSE_DOC)
def run_events(
    run_id: UUID,
    service: Service,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replay events after ``after`` (or ``Last-Event-ID``), then follow live events.
    Requires a valid session and current authorization; content is re-authorized."""

    _require_available(service)
    token = _token(credentials)
    if last_event_id is not None and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    try:
        ctx = service.open_stream(token, run_id)
    except RunNotFound as exc:
        raise _not_found() from exc
    except Exception as exc:
        raise unauthenticated() from exc
    return StreamingResponse(
        service.stream(token, ctx, run_id, after),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Run-ID": str(run_id)},
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunOut,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_run(run_id: UUID, ctx: CurrentContext, service: Service) -> RunOut:
    try:
        run, content_allowed = service.get_run(ctx, run_id)
    except RunNotFound as exc:
        raise _not_found() from exc
    return RunOut(
        run_id=run.id,
        status=run.status.value,
        inference_mode=run.inference_mode,
        outcome_code=run.outcome_code if content_allowed else "access_revoked",
        created_at=run.created_at,
        finished_at=run.finished_at,
        last_event_id=run.last_seq,
        diagnostics=_diagnostics(run.stats) if content_allowed else None,
        result=run.result if content_allowed else None,
    )


_DIAGNOSTIC_KEYS = (
    "timings",
    "sql_attempts",
    "report_attempts",
    "max_repairs",
    "model_calls",
    "model",
    "served_models",
    "prompt_version",
    "profile_version",
    "checker_scores",
    "rejections",
    "model_errors",
)


def _diagnostics(stats: dict[str, Any]) -> dict[str, Any]:
    return {key: stats[key] for key in _DIAGNOSTIC_KEYS if key in stats}


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def cancel_run(run_id: UUID, ctx: CurrentContext, service: Service) -> RunOut:
    try:
        run = service.cancel(ctx, run_id)
    except RunNotFound as exc:
        raise _not_found() from exc
    return RunOut(
        run_id=run.id,
        status=run.status.value,
        inference_mode=run.inference_mode,
        outcome_code=run.outcome_code,
        created_at=run.created_at,
        finished_at=run.finished_at,
        last_event_id=run.last_seq,
        result=None,
    )


@router.get(
    "/evidence/{evidence_id}",
    response_model=EvidenceOut,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_evidence(evidence_id: UUID, ctx: CurrentContext, service: Service) -> EvidenceOut:
    """Exact stored query result behind a report's cells and citations. Owner only,
    published runs only, and only while the caller is still authorized."""

    try:
        return EvidenceOut.model_validate(service.evidence(ctx, evidence_id))
    except RunNotFound as exc:
        raise _not_found() from exc
