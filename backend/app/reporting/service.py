"""Report runs: creation with a durable job row, dispatch, execution, cancellation,
authorized event streaming/replay and authorized evidence access."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, text

from app.config import ModelMode, ReportDispatch, Settings
from app.db.engine import contextless_transaction, tenant_transaction
from app.reporting import store
from app.reporting.access import authorize_dataset
from app.reporting.contracts import (
    CONTENT_EVENTS,
    ERROR_MESSAGES,
    STEP_LABELS,
    TERMINAL_STATUSES,
    ErrorCode,
    EventType,
    RunStatus,
    Step,
)
from app.reporting.model_client import FAKE_LABEL
from app.reporting.workflow import ReportWorkflow
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext
from app.security.policy import Decision, Reason
from app.tenancy.config import RUN_KIND_PACK, load_settings, require_pack

logger = logging.getLogger("eaios.reports")


class RunNotFound(Exception):
    """Unknown, foreign or no-longer-authorized run or evidence (uniform 404)."""


class AccessLost(Exception):
    """Authentication or authorization failed during a stream."""


class Dispatcher(Protocol):
    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None: ...


@dataclass
class ReportRunner:
    """Executes one job. Safe under duplicate delivery: only one worker can claim a
    job at a time, and a run that already reached a terminal state is not re-run."""

    app_engine: Engine
    workflow: ReportWorkflow
    settings: Settings
    documents_workflow: ReportWorkflow | None = None

    def run_job(self, run_id: UUID, tenant_id: UUID) -> str:
        with tenant_transaction(self.app_engine, tenant_id) as conn:
            run = store.load_run(conn, run_id)
            if run is None:
                return "missing"
            if run.status in TERMINAL_STATUSES:
                store.finish_job(conn, run_id)
                return "already_finished"
            attempt = store.claim_job(conn, run_id, self.settings.report_job_lease_seconds)
            if attempt is None:
                return "not_claimed"
        if attempt > self.settings.report_job_max_attempts:
            self._fail_exhausted(run_id, tenant_id, run.owner_user_id)
            return "attempts_exhausted"
        workflow = self.workflow
        if run.kind == "documents":
            if self.documents_workflow is None:
                self._fail_exhausted(run_id, tenant_id, run.owner_user_id)
                return "documents_unavailable"
            workflow = self.documents_workflow
        workflow.run(run_id, tenant_id, run.owner_user_id, run.question, run.inference_mode)
        return "ran"

    def _fail_exhausted(self, run_id: UUID, tenant_id: UUID, user_id: UUID) -> None:
        with tenant_transaction(self.app_engine, tenant_id, user_id) as conn:
            run = store.lock_run(conn, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            store.append_event(
                conn,
                run_id,
                tenant_id,
                EventType.ERROR,
                {
                    "code": ErrorCode.INTERNAL.value,
                    "message": ERROR_MESSAGES[ErrorCode.INTERNAL],
                },
            )
            store.append_event(
                conn,
                run_id,
                tenant_id,
                EventType.DONE,
                {
                    "message_id": None,
                    "outcome": "failed",
                    "verification": None,
                    "abstention": None,
                    "inference": run.inference_mode,
                },
            )
            store.update_run(
                conn,
                run_id,
                status=RunStatus.FAILED,
                outcome_code="job_attempts_exhausted",
                finished_at="now",
            )
            store.finish_job(conn, run_id)


class JobRunner(Protocol):
    def run_job(self, job_id: UUID, tenant_id: UUID) -> str: ...


class InlineDispatcher:
    """Runs jobs on a small thread pool inside the API process (tests, single-process
    development). The job row is still the source of truth. Used for report/answer
    runs and for ingestion jobs (any runner with ``run_job(id, tenant_id)``)."""

    def __init__(self, runner: JobRunner, workers: int = 2) -> None:
        self._runner = runner
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report")

    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None:
        self._pool.submit(self._safe, run_id, tenant_id)

    def _safe(self, run_id: UUID, tenant_id: UUID) -> None:
        try:
            self._runner.run_job(run_id, tenant_id)
        except Exception:
            logger.exception("inline_report_job_failed")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


class CeleryDispatcher:
    """Publishes a Celery message after the run/job transaction committed. If the
    publish fails, the beat task re-dispatches pending jobs from PostgreSQL."""

    def __init__(
        self,
        app_engine: Engine,
        send: Callable[[str, list[str]], None],
        task: str = "app.tasks.reports.run_report",
        mark: Callable[[Any, UUID], None] = store.mark_dispatched,
    ) -> None:
        self._engine = app_engine
        self._send = send
        self._task = task
        self._mark = mark

    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None:
        try:
            self._send(self._task, [str(run_id), str(tenant_id)])
        except Exception:
            logger.warning("job_dispatch_failed", extra={"job_id": str(run_id), "task": self._task})
            return
        with tenant_transaction(self._engine, tenant_id) as conn:
            self._mark(conn, run_id)


def dispatch_due_jobs(app_engine: Engine, dispatcher: Dispatcher, limit: int = 20) -> int:
    """Re-dispatch pending jobs not dispatched recently and lease-expired claims."""

    with contextless_transaction(app_engine) as conn:
        due = conn.execute(
            text("SELECT run_id, tenant_id FROM report_jobs_due(:n, interval '30 seconds')"),
            {"n": limit},
        ).all()
    for row in due:
        dispatcher.dispatch(row.run_id, row.tenant_id)
    return len(due)


def sse_frame(seq: int | None, event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


class ReportService:
    def __init__(
        self,
        app_engine: Engine,
        settings: Settings,
        dispatcher: Dispatcher | None,
        reauthenticate: Callable[[str], AccessContext],
    ) -> None:
        self._engine = app_engine
        self._settings = settings
        self._dispatcher = dispatcher
        self._reauthenticate = reauthenticate

    @property
    def available(self) -> bool:
        return self._dispatcher is not None

    @property
    def inference_mode(self) -> str:
        return self._settings.model_mode.value

    def enabled_packs(self, ctx: AccessContext) -> frozenset[str]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            return load_settings(conn).enabled_packs

    def create_run(
        self, ctx: AccessContext, question: str, client_request_id: str, kind: str = "report"
    ) -> store.RunRow:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            require_pack(conn, RUN_KIND_PACK[kind])  # M4: backend-enforced tenant packs
            run, created = store.create_run(
                conn,
                tenant_id=ctx.tenant_id,
                owner_user_id=ctx.user_id,
                question=question,
                client_request_id=client_request_id,
                inference_mode=self.inference_mode,
                policy_epoch=ctx.policy_epoch,
                kind=kind,
            )
            if created:
                payload: dict[str, Any] = {
                    "step": Step.ACCEPTED.value,
                    "state": "completed",
                    "label": STEP_LABELS[Step.ACCEPTED],
                    "run_id": str(run.id),
                    "inference": self.inference_mode,
                }
                if self.inference_mode == ModelMode.FAKE.value:
                    payload["inference_label"] = FAKE_LABEL
                store.append_event(conn, run.id, ctx.tenant_id, EventType.STATUS, payload)
                record_audit_event(
                    conn,
                    request_id=ctx.request_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    event_type="report.run",
                    action="report.create",
                    decision="info",
                    reason="run_created",
                    details={"run_id": str(run.id), "inference": self.inference_mode, "kind": kind},
                )
        if created and self._dispatcher is not None:
            self._dispatcher.dispatch(run.id, ctx.tenant_id)
        return run

    def get_run(self, ctx: AccessContext, run_id: UUID) -> tuple[store.RunRow, bool]:
        """Return the owner's run and whether its published content may be shown now."""

        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            content_allowed = True
            if run.status is RunStatus.PUBLISHED:
                content_allowed = self._content_allowed(conn, ctx.user_id, run)
        return run, content_allowed

    def cancel(self, ctx: AccessContext, run_id: UUID) -> store.RunRow:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            if run.status not in TERMINAL_STATUSES:
                run = store.request_cancel(conn, run_id, ctx.user_id) or run
                record_audit_event(
                    conn,
                    request_id=ctx.request_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    event_type="report.run",
                    action="report.cancel",
                    decision="info",
                    reason="cancel_requested",
                    details={"run_id": str(run_id)},
                )
        return run

    # -- content authorization -----------------------------------------------------

    @staticmethod
    def _content_decision(conn: Any, user_id: UUID, run: store.RunRow) -> Decision:
        """Current authorization for a run's published content.

        Reports: ``dataset.query`` on the report's dataset. Document answers:
        ``resource.read`` on every cited document, which must not be deleted.
        """

        if run.kind != "documents":
            return authorize_dataset(conn, user_id, run.resource_id)
        from app.documents import access as doc_access  # noqa: PLC0415 - avoid cycle
        from app.documents import store as doc_store  # noqa: PLC0415

        cited = (
            conn.execute(
                text("SELECT DISTINCT document_id FROM answer_sources WHERE run_id = :r"),
                {"r": run.id},
            )
            .scalars()
            .all()
        )
        for document_id in cited:
            doc = doc_store.load_document(conn, document_id)
            if doc is None or doc.deleted_at is not None:
                return Decision.deny(Reason.RESOURCE_INACTIVE)
            decision = doc_access.can_read(conn, user_id, document_id)
            if not decision.allowed:
                return decision
        return Decision.allow()

    def _content_allowed(self, conn: Any, user_id: UUID, run: store.RunRow) -> bool:
        return self._content_decision(conn, user_id, run).allowed

    # -- streaming -----------------------------------------------------------------

    def _check_access(self, token: str, run_id: UUID, content: bool) -> AccessContext:
        """Fresh authentication and, for content, fresh dataset authorization."""

        try:
            ctx = self._reauthenticate(token)
        except Exception as exc:
            raise AccessLost("session") from exc
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            if content:
                decision = self._content_decision(conn, ctx.user_id, run)
                if not decision.allowed:
                    record_audit_event(
                        conn,
                        request_id=ctx.request_id,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        event_type="report.replay",
                        action="dataset.query",
                        resource_id=run.resource_id,
                        decision="deny",
                        reason=decision.reason.value,
                        details={"run_id": str(run_id)},
                    )
                    raise AccessLost(decision.reason.value)
        return ctx

    def open_stream(self, token: str, run_id: UUID) -> AccessContext:
        """Authorize before any byte is streamed (raises RunNotFound for foreign runs)."""

        return self._check_access(token, run_id, content=False)

    @staticmethod
    def _carries_content(event: store.EventRow) -> bool:
        return event.event_type in CONTENT_EVENTS or (
            event.event_type is EventType.DONE and event.payload.get("outcome") == "published"
        )

    @staticmethod
    def _revoked_frame() -> str:
        return sse_frame(
            None,
            EventType.ERROR.value,
            {
                "code": ErrorCode.ACCESS_REVOKED.value,
                "message": ERROR_MESSAGES[ErrorCode.ACCESS_REVOKED],
            },
        )

    def stream(self, token: str, ctx: AccessContext, run_id: UUID, after: int) -> Iterator[str]:
        """Yield SSE frames from ``after`` until the terminal ``done`` event.

        Only persisted events are sent, so the stream and a later replay are identical.
        Every content-bearing event (``token``, ``citation``) and a published ``done`` is
        re-authorized individually, from current PostgreSQL state, immediately before it
        is yielded, including events that arrived in the same fetched batch. The check
        runs when the consumer asks for the next frame, so a revocation between two
        frames stops the stream with one ``error`` (``access_revoked``) and nothing else.
        """

        s = self._settings
        cursor = after
        started = last_beat = last_auth = time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            now = time.monotonic()
            if now - started > s.sse_max_stream_seconds:
                return
            try:
                if now - last_auth > 5.0:
                    self._check_access(token, run_id, content=False)
                    last_auth = now
                with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
                    events = store.events_after(conn, run_id, cursor)
            except (AccessLost, RunNotFound):
                yield self._revoked_frame()
                return
            for event in events:
                if self._carries_content(event):
                    try:
                        self._check_access(token, run_id, content=True)
                    except (AccessLost, RunNotFound):
                        yield self._revoked_frame()
                        return
                    last_auth = time.monotonic()
                cursor = event.seq
                yield sse_frame(
                    event.seq,
                    event.event_type.value,
                    {"run_id": str(run_id), "version": event.version, **event.payload},
                )
                if event.event_type is EventType.DONE:
                    return
            if not events:
                if time.monotonic() - last_beat >= s.sse_heartbeat_seconds:
                    last_beat = time.monotonic()
                    yield ": heartbeat\n\n"
                time.sleep(s.sse_poll_interval_seconds)

    # -- document citations ------------------------------------------------------

    def citation(self, ctx: AccessContext, run_id: UUID, citation_id: str) -> dict[str, Any]:
        """Open a cited passage in the exact document version the answer used.

        Owner only, published runs only, document not deleted, and ``resource.read``
        re-evaluated now. The passage is sliced by code points on the server, so the
        browser never converts offsets (JavaScript strings index UTF-16 units).
        """

        from app.documents import access as doc_access  # noqa: PLC0415 - avoid cycle
        from app.documents import store as doc_store  # noqa: PLC0415

        context_chars = 240
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            source = doc_store.load_source(conn, run_id, citation_id) if run else None
            allowed, reason = False, "not_found"
            doc = version = None
            if run is not None and run.status is RunStatus.PUBLISHED and source is not None:
                doc = doc_store.load_document(conn, source.document_id)
                version = doc_store.load_version(conn, source.document_id, source.version)
                if doc is None or doc.deleted_at is not None or version is None:
                    reason = "document_removed"
                else:
                    decision = doc_access.can_read(conn, ctx.user_id, source.document_id)
                    allowed, reason = decision.allowed, decision.reason.value
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="document.citation",
                action="resource.read",
                resource_id=source.document_id if source and allowed else None,
                decision="allow" if allowed else "deny",
                reason=reason,
                details={"run_id": str(run_id), "citation_id": citation_id},
            )
        if not allowed or source is None or doc is None or version is None:
            raise RunNotFound
        canonical = version.canonical_text or ""
        quote = canonical[source.char_start : source.char_end]
        integrity = (
            hashlib.sha256(canonical.encode()).hexdigest()
            == source.text_sha256
            == version.text_sha256
            and hashlib.sha256(quote.encode()).hexdigest() == source.quote_sha256
        )
        before_start = max(0, source.char_start - context_chars)
        after_end = min(len(canonical), source.char_end + context_chars)
        return {
            "run_id": str(run_id),
            "citation_id": citation_id,
            "document_id": str(source.document_id),
            "title": doc.title,
            "version": source.version,
            "current_version": doc.current_version,
            "is_current_version": source.version == doc.current_version,
            "filename": version.filename,
            "char_start": source.char_start,
            "char_end": source.char_end,
            "offset_unit": "unicode_code_point",
            "text_sha256": source.text_sha256,
            "before": canonical[before_start : source.char_start],
            "quote": quote,
            "after": canonical[source.char_end : after_end],
            "context_start": before_start,
            "integrity": "verified" if integrity else "mismatch",
        }

    # -- evidence ------------------------------------------------------------------

    def evidence(self, ctx: AccessContext, evidence_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            row = store.load_evidence(conn, evidence_id, ctx.user_id)
            run = store.load_run(conn, row.run_id, ctx.user_id) if row else None
            decision_reason = "not_found"
            allowed = False
            if row is not None and run is not None and run.status is RunStatus.PUBLISHED:
                decision = authorize_dataset(conn, ctx.user_id, row.resource_id)
                allowed, decision_reason = decision.allowed, decision.reason.value
            elif row is not None:
                decision_reason = "run_not_published"
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="report.evidence",
                action="evidence.read",
                resource_id=row.resource_id if row and allowed else None,
                decision="allow" if allowed else "deny",
                reason=decision_reason,
                details={"evidence_id": str(evidence_id)},
            )
        if not allowed or row is None:
            raise RunNotFound
        recomputed = row.recomputed_sha256()
        return {
            "evidence_id": str(row.id),
            "run_id": str(row.run_id),
            "report_key": row.report_key,
            "executed_sql": row.executed_sql,
            "parameters": row.parameters,
            "columns": row.columns,
            "rows": row.rows,
            "row_count": row.row_count,
            "semantics": row.semantics,
            "dataset_fingerprint": row.dataset_fingerprint,
            "policy_epoch": row.policy_epoch,
            "executed_at": row.executed_at.isoformat(),
            "result_sha256": row.result_sha256,
            "integrity": "verified" if recomputed == row.result_sha256 else "mismatch",
        }


def build_dispatcher(
    settings: Settings,
    runner: JobRunner | None,
    app_engine: Engine,
    task: str = "app.tasks.reports.run_report",
    mark: Callable[[Any, UUID], None] = store.mark_dispatched,
) -> Dispatcher | None:
    if settings.report_dispatch is ReportDispatch.INLINE:
        return InlineDispatcher(runner) if runner is not None else None
    # Imported lazily: the Celery app reads REDIS_URL at import time.
    from app.tasks.worker import celery_app  # noqa: PLC0415

    celery_app.conf.broker_url = settings.redis_url.get_secret_value()

    def send(name: str, args: list[str]) -> None:
        celery_app.send_task(name, args=args, queue="default")

    return CeleryDispatcher(app_engine, send, task, mark)
