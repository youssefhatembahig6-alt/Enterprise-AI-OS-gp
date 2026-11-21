"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis

from app.admin.service import AdminService
from app.api import admin, auth, documents, health, reports
from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.connectors.drive import ConnectorConfig, DriveClientFactory, http_client_factory
from app.connectors.sync import DriveSyncService, SyncOutcome
from app.db.engine import create_app_engine
from app.documents import store as doc_store
from app.documents.ingest import DocumentService
from app.documents.qdrant import QdrantClient
from app.logging_setup import configure_logging, request_id_var
from app.reporting.factory import build_components, build_object_store
from app.reporting.model_client import ModelClient, build_model_client
from app.reporting.service import InlineDispatcher, ReportService, build_dispatcher
from app.security.passwords import PasswordVerifier
from app.security.ratelimit import LoginRateLimiter
from app.services.health import ReadinessChecker
from app.services.identity import IdentityService


def create_app(  # noqa: PLR0915
    settings: Settings | None = None,
    model: ModelClient | None = None,
    drive_clients: DriveClientFactory | None = None,
) -> FastAPI:
    """Build the API. ``model`` and ``drive_clients`` override the model client and the
    Google Drive client factory (tests inject doubles)."""

    settings = settings or get_settings()
    configure_logging()

    engine = create_app_engine(
        settings.database_url.get_secret_value(), pool_size=settings.database_pool_size
    )
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.readiness_timeout_seconds,
        socket_connect_timeout=settings.readiness_timeout_seconds,
    )

    # Runs (reports and document answers) and ingestion. With inline dispatch the API
    # executes jobs on its own thread pools; with Celery the worker does and the API
    # only writes job rows and publishes messages.
    model_client = model or build_model_client(settings)
    objects = build_object_store(settings)
    qdrant = QdrantClient(settings.qdrant_url)
    components = None
    if settings.report_dispatch.value == "inline":
        components = build_components(settings, engine, model_client, objects, qdrant)
    runner = components.runner if components else None
    dispatcher = build_dispatcher(settings, runner, engine)
    ingest_dispatcher = build_dispatcher(
        settings,
        components.ingestion if components else None,
        engine,
        task="app.tasks.documents.ingest_document",
        mark=doc_store.mark_job_dispatched,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for pool in (dispatcher, ingest_dispatcher):
            if isinstance(pool, InlineDispatcher):
                pool.shutdown()
        if components is not None:
            components.close()
        else:
            qdrant.close()
        engine.dispose()
        redis.close()  # type: ignore[no-untyped-call]

    app = FastAPI(
        title="Enterprise AI OS API",
        version="0.4.0-m4",
        description=(
            "Identity, tenant isolation and health (M0); verified SQL reports (M2) and "
            "authorized document answers with exact citations (M3), streamed over "
            "Server-Sent Events; Google Drive sync, scoped administration and tenant "
            "configuration (M4)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    limiter = LoginRateLimiter(
        redis,
        prefix=settings.rate_limit_key_prefix,
        max_per_identity=settings.login_max_failures_per_identity,
        max_per_client=settings.login_max_failures_per_client,
        window_seconds=settings.login_failure_window_seconds,
    )
    verifier = PasswordVerifier(
        concurrency=settings.password_verify_concurrency,
        queue_timeout_seconds=settings.password_verify_queue_timeout_seconds,
    )
    identity = IdentityService(engine, settings, limiter, verifier)
    app.state.identity = identity
    app.state.reports = ReportService(
        engine,
        settings,
        dispatcher,
        reauthenticate=lambda token: identity.authenticate(token, request_id_var.get()),
    )
    app.state.report_runner = runner
    app.state.ingestion_runner = components.ingestion if components else None
    app.state.documents = DocumentService(
        engine,
        settings,
        objects,
        qdrant,
        model_client,
        ingest_dispatcher.dispatch if ingest_dispatcher else None,
    )
    # M4: Drive sync. Inline dispatch runs a manual sync in the request (tests and
    # single-process development); Celery queues it for the worker.
    drive_config = ConnectorConfig(settings.drive_connector_config)
    drive_sync = DriveSyncService(
        engine,
        settings,
        objects,
        qdrant,
        model_client,
        drive_clients or http_client_factory(drive_config),
        ingest_dispatcher.dispatch if ingest_dispatcher else None,
        lease_seconds=settings.drive_sync_lease_seconds,
        roots=drive_config.allowed_roots,
    )
    app.state.drive_sync = drive_sync

    def run_sync(
        tenant_id: uuid.UUID, source_id: uuid.UUID, user_id: uuid.UUID, run_id: uuid.UUID
    ) -> SyncOutcome | None:
        if settings.report_dispatch.value == "inline":
            return drive_sync.sync(
                tenant_id, source_id, trigger="manual", requested_by=user_id, run_id=run_id
            )
        from app.tasks.worker import celery_app  # noqa: PLC0415

        celery_app.conf.broker_url = settings.redis_url.get_secret_value()
        celery_app.send_task(
            "app.tasks.connectors.sync_source",
            args=[str(source_id), str(tenant_id), "manual", str(user_id), str(run_id)],
            queue="default",
        )
        return None

    app.state.admin = AdminService(engine, drive_config, run_sync)
    app.state.readiness = ReadinessChecker(
        engine=engine,
        redis=redis,
        qdrant_url=settings.qdrant_url,
        s3_endpoint_url=settings.s3_endpoint_url,
        timeout=settings.readiness_timeout_seconds,
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Always server-generated; an inbound X-Request-ID is not trusted.
        request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID", "Retry-After", "X-Run-ID"],
        max_age=600,
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(reports.router)
    app.include_router(documents.router)
    app.include_router(admin.router)
    return app
