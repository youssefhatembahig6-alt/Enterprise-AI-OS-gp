"""Wiring for run execution (reports and document answers) and document ingestion.

Used by the API (inline dispatch) and the Celery worker. The report runner no longer
requires ``REPORT_DATABASE_URL``: without it, report runs end with
``reports_unavailable`` while document answers still work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine

from app.config import DocumentStoreKind, Settings
from app.connectors.drive import ConnectorConfig, DriveClientFactory, http_client_factory
from app.connectors.sync import DriveSyncService
from app.documents.answers import DocumentAnswerWorkflow
from app.documents.ingest import IngestionRunner, collection_name
from app.documents.qdrant import QdrantClient
from app.documents.storage import FileSystemStore, ObjectStore, S3Store
from app.reporting.executor import ReportExecutor, create_report_engine
from app.reporting.model_client import ModelClient, build_model_client
from app.reporting.service import ReportRunner
from app.reporting.workflow import ReportWorkflow, WorkflowDeps


@dataclass
class Components:
    model: ModelClient
    objects: ObjectStore
    qdrant: QdrantClient
    runner: ReportRunner
    ingestion: IngestionRunner
    report_engine: Engine | None
    drive_sync: DriveSyncService | None = None

    def close(self) -> None:
        self.qdrant.close()
        if self.report_engine is not None:
            self.report_engine.dispose()


def build_object_store(settings: Settings) -> ObjectStore:
    if settings.document_store is DocumentStoreKind.FILESYSTEM:
        return FileSystemStore(Path(settings.document_fs_root))
    return S3Store(
        settings.s3_endpoint_url,
        settings.s3_bucket,
        settings.s3_access_key.get_secret_value(),
        settings.s3_secret_key.get_secret_value(),
    )


def build_components(
    settings: Settings,
    app_engine: Engine,
    model: ModelClient | None = None,
    objects: ObjectStore | None = None,
    qdrant: QdrantClient | None = None,
    drive_clients: DriveClientFactory | None = None,
    ingest_dispatch: Callable[[UUID, UUID], None] | None = None,
) -> Components:
    model = model or build_model_client(settings)
    objects = objects or build_object_store(settings)
    qdrant = qdrant or QdrantClient(settings.qdrant_url)
    report_engine = None
    executor = None
    if settings.report_database_url is not None:
        report_engine = create_report_engine(
            settings.report_database_url.get_secret_value(), pool_size=settings.report_pool_size
        )
        executor = ReportExecutor(
            app_engine,
            report_engine,
            statement_timeout_ms=settings.report_statement_timeout_ms,
            max_rows=settings.report_max_rows,
        )
    deps = WorkflowDeps(app_engine=app_engine, executor=executor, model=model, settings=settings)
    documents = DocumentAnswerWorkflow(
        WorkflowDeps(app_engine=app_engine, executor=None, model=model, settings=settings),
        qdrant,
        lambda: collection_name(settings, model),
    )
    runner = ReportRunner(app_engine, ReportWorkflow(deps), settings, documents)
    ingestion = IngestionRunner(app_engine, settings, objects, qdrant, model)
    connector_config = ConnectorConfig(settings.drive_connector_config)
    clients = drive_clients or http_client_factory(connector_config)
    drive_sync = DriveSyncService(
        app_engine,
        settings,
        objects,
        qdrant,
        model,
        clients,
        ingest_dispatch,
        lease_seconds=settings.drive_sync_lease_seconds,
        roots=connector_config.allowed_roots,
    )
    return Components(model, objects, qdrant, runner, ingestion, report_engine, drive_sync)


def build_runner(
    settings: Settings, app_engine: Engine, model: ModelClient | None = None
) -> tuple[ReportRunner, Engine] | None:
    """Backward-compatible helper used by M2 code paths (report role required)."""

    if settings.report_database_url is None:
        return None
    components = build_components(settings, app_engine, model)
    assert components.report_engine is not None  # noqa: S101
    return components.runner, components.report_engine
