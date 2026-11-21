"""Celery tasks for connector sync (M4). The beat task asks PostgreSQL which sources
are due (``connector_sources_due``: 15 minutes in demo mode, nightly after 02:00 UTC
otherwise) and dispatches one sync per source; the sync claim (lease on the source
row) makes duplicate deliveries no-ops."""

from __future__ import annotations

import logging
import os
import threading
from uuid import UUID

from sqlalchemy import text

from app.config import get_settings
from app.connectors.sync import DriveSyncService
from app.db.engine import contextless_transaction, create_app_engine
from app.documents import store as doc_store
from app.logging_setup import configure_logging
from app.reporting.factory import build_components
from app.reporting.service import CeleryDispatcher
from app.tasks.worker import celery_app

logger = logging.getLogger("eaios.tasks.connectors")
_lock = threading.Lock()
_service: DriveSyncService | None = None


def _get_service() -> DriveSyncService:
    global _service  # noqa: PLW0603 - one service (and pools) per worker process
    with _lock:
        if _service is None:
            configure_logging()
            settings = get_settings()
            engine = create_app_engine(settings.database_url.get_secret_value(), pool_size=2)

            def send(name: str, args: list[str]) -> None:
                celery_app.send_task(name, args=args, queue="default")

            ingest = CeleryDispatcher(
                engine, send, "app.tasks.documents.ingest_document", doc_store.mark_job_dispatched
            )
            service = build_components(settings, engine, ingest_dispatch=ingest.dispatch).drive_sync
            assert service is not None  # noqa: S101
            _service = service
        return _service


@celery_app.task(  # type: ignore[misc]
    name="app.tasks.connectors.sync_source", time_limit=1800, soft_time_limit=1700, acks_late=True
)
def sync_source(
    source_id: str,
    tenant_id: str,
    trigger: str = "scheduled",
    requested_by: str | None = None,
    run_id: str | None = None,
) -> str:
    outcome = _get_service().sync(
        UUID(tenant_id),
        UUID(source_id),
        trigger=trigger,
        requested_by=UUID(requested_by) if requested_by else None,
        run_id=UUID(run_id) if run_id else None,  # the queued row the API created
    )
    logger.info("connector_sync", extra={"status": outcome.status, "code": outcome.error_code})
    return outcome.status


@celery_app.task(name="app.tasks.connectors.dispatch_due_syncs")  # type: ignore[misc]
def dispatch_due_syncs() -> int:
    configure_logging()
    engine = create_app_engine(os.environ["DATABASE_URL"], pool_size=1)
    try:
        with contextless_transaction(engine) as conn:
            due = conn.execute(
                text("SELECT source_id, tenant_id FROM connector_sources_due(20)")
            ).all()
        for row in due:
            celery_app.send_task(
                "app.tasks.connectors.sync_source",
                args=[str(row.source_id), str(row.tenant_id)],
                queue="default",
            )
        return len(due)
    finally:
        engine.dispose()
