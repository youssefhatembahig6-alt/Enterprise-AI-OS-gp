"""Celery application (worker and beat share this module).

Maintenance tasks live here; report tasks are in ``app.tasks.reports``. M0 has one
real maintenance task: purging long-expired session rows through the
restricted ``purge_expired_sessions()`` database function.
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from sqlalchemy import text

from app.db.engine import create_app_engine
from app.logging_setup import configure_logging

logger = logging.getLogger("eaios.tasks")


def _broker_url() -> str | None:
    # Worker and beat always run with REDIS_URL. The API sets the broker from its own
    # settings when it builds a dispatcher (app.reporting.service.build_dispatcher).
    return os.environ.get("REDIS_URL")


celery_app = Celery(
    "eaios",
    broker=_broker_url(),
    include=[
        "app.tasks.worker",
        "app.tasks.reports",
        "app.tasks.documents",
        "app.tasks.connectors",
    ],
)
celery_app.conf.update(
    task_default_queue="default",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=60,
    task_soft_time_limit=45,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "purge-expired-sessions": {
            "task": "app.tasks.worker.purge_expired_sessions",
            "schedule": 3600.0,
        },
        # Outbox: re-dispatch report jobs whose publish failed or whose lease expired.
        "dispatch-due-report-jobs": {
            "task": "app.tasks.reports.dispatch_due_report_jobs",
            "schedule": 30.0,
        },
        "dispatch-due-ingestion-jobs": {
            "task": "app.tasks.documents.dispatch_due_ingestion_jobs",
            "schedule": 30.0,
        },
        # M4: checks every minute which connector sources are due (15 min in demo
        # mode, nightly otherwise); the schedule itself lives in tenant_settings.
        "dispatch-due-connector-syncs": {
            "task": "app.tasks.connectors.dispatch_due_syncs",
            "schedule": 60.0,
        },
    },
)


@celery_app.task(name="app.tasks.worker.purge_expired_sessions")  # type: ignore[misc]
def purge_expired_sessions() -> int:
    configure_logging()
    engine = create_app_engine(os.environ["DATABASE_URL"], pool_size=1)
    try:
        with engine.begin() as conn:
            removed = int(conn.execute(text("SELECT purge_expired_sessions()")).scalar_one())
    finally:
        engine.dispose()
    logger.info("purged_expired_sessions", extra={"removed": removed})
    return removed
