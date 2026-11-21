"""Real PostgreSQL + Redis fixtures.

Each test session creates its own disposable database and its own owner/app
roles (random names and passwords) using TEST_DATABASE_ADMIN_URL, runs the real
Alembic migrations as the owner role, seeds the foundation fixtures, and
connects the application as the non-owner app role. Everything is dropped at
the end. Set EAIOS_REQUIRE_INTEGRATION=1 (CI does) to fail instead of skip when
the services are not configured.
"""

from __future__ import annotations

import os
import secrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db.engine import create_app_engine
from app.db.roles import report_role_settings_sql
from app.fixtures import Foundation, load_foundation
from app.main import create_app
from app.seed import seed_foundation

ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
JWT_SECRET = "integration-" + secrets.token_urlsafe(32)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def _require_services() -> None:
    if ADMIN_URL and REDIS_URL:
        return
    message = "TEST_DATABASE_ADMIN_URL and TEST_REDIS_URL are required for integration tests"
    if os.environ.get("EAIOS_REQUIRE_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


@dataclass
class DbHarness:
    name: str
    owner_role: str
    app_role: str
    report_role: str
    owner_url: str
    app_url: str
    report_url: str
    owner_engine: Engine
    foundation: Foundation
    passwords: dict[str, dict[str, str]]
    secrets: list[str]

    def password(self, ref: str) -> str:
        slug, key = ref.split("/", 1)
        email = self.foundation.tenants[slug].users[key].email
        return self.passwords[slug][email]


def _with_credentials(base: str, user: str, password: str, database: str) -> str:
    url = make_url(base).set(username=user, password=password, database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_db() -> Iterator[DbHarness]:
    with disposable_database(seed=True) as db:
        yield db


@contextmanager
def disposable_database(*, seed: bool = True, revision: str = "head") -> Iterator[DbHarness]:
    """Create a fresh database plus owner/app roles, migrate it (to ``revision``),
    optionally seed M0. ``migrate(db, "head")`` continues an older database."""

    assert ADMIN_URL
    suffix = uuid.uuid4().hex[:10]
    name = f"eaios_test_{suffix}"
    owner_role, app_role = f"eaios_t_owner_{suffix}", f"eaios_t_app_{suffix}"
    report_role = f"eaios_t_rep_{suffix}"
    owner_pw, app_pw = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    report_pw = secrets.token_urlsafe(24)

    admin = create_engine(ADMIN_URL, poolclass=pool.NullPool, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(
            f"CREATE ROLE {owner_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            f" PASSWORD '{owner_pw}'"
        )
        conn.exec_driver_sql(
            f"CREATE ROLE {app_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            f" NOINHERIT PASSWORD '{app_pw}'"
        )
        # The separate report query role, created exactly as init-roles.sh does.
        conn.exec_driver_sql(
            f"CREATE ROLE {report_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            f" NOINHERIT CONNECTION LIMIT 8 PASSWORD '{report_pw}'"
        )
        for statement in report_role_settings_sql(report_role):
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql(f"CREATE DATABASE {name} OWNER {owner_role}")
        conn.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
        conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {name} TO {app_role}")
        conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {name} TO {report_role}")
    # PostgreSQL 15+: public schema is owned by pg_database_owner (the owner role).
    db_admin = create_engine(
        _with_credentials(ADMIN_URL, *_admin_creds(), name),
        poolclass=pool.NullPool,
        isolation_level="AUTOCOMMIT",
    )
    with db_admin.connect() as conn:
        conn.exec_driver_sql("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    db_admin.dispose()

    owner_url = _with_credentials(ADMIN_URL, owner_role, owner_pw, name)
    app_url = _with_credentials(ADMIN_URL, app_role, app_pw, name)
    report_url = _with_credentials(ADMIN_URL, report_role, report_pw, name)

    _upgrade(owner_url, app_role, report_role, revision)

    owner_engine = create_engine(owner_url, poolclass=pool.NullPool)
    foundation = load_foundation()
    credentials: dict[str, dict[str, str]] = {}
    if seed:
        credentials = seed_foundation(owner_engine, foundation, environment="test").new_credentials
    all_passwords = [pw for users in credentials.values() for pw in users.values()]
    db = DbHarness(
        name=name,
        owner_role=owner_role,
        app_role=app_role,
        report_role=report_role,
        owner_url=owner_url,
        app_url=app_url,
        report_url=report_url,
        owner_engine=owner_engine,
        foundation=foundation,
        passwords=credentials,
        secrets=[owner_pw, app_pw, report_pw, JWT_SECRET, *all_passwords],
    )
    try:
        yield db
    finally:
        owner_engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            conn.exec_driver_sql(f"DROP ROLE IF EXISTS {app_role}")
            conn.exec_driver_sql(f"DROP ROLE IF EXISTS {report_role}")
            conn.exec_driver_sql(f"DROP ROLE IF EXISTS {owner_role}")
        admin.dispose()


def _upgrade(owner_url: str, app_role: str, report_role: str, revision: str) -> None:
    role_env = {"EAIOS_APP_DB_ROLE": app_role, "EAIOS_REPORT_DB_ROLE": report_role}
    previous = {key: os.environ.get(key) for key in role_env}
    os.environ.update(role_env)
    try:
        config = Config("alembic.ini")
        config.attributes["url"] = owner_url
        command.upgrade(config, revision)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def migrate(db: DbHarness, revision: str = "head") -> None:
    """Upgrade an existing disposable database (upgrade tests)."""

    _upgrade(db.owner_url, db.app_role, db.report_role, revision)


def _admin_creds() -> tuple[str, str]:
    assert ADMIN_URL
    url = make_url(ADMIN_URL)
    return str(url.username), str(url.password)


@pytest.fixture(scope="session")
def app_engine(test_db: DbHarness) -> Iterator[Engine]:
    engine = create_app_engine(test_db.app_url, pool_size=2)
    yield engine
    engine.dispose()


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 200 if self.path == "/readyz" else 403
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture(scope="session")
def stub_http_url() -> Iterator[str]:
    """Stands in for Qdrant (/readyz -> 200) and the S3 endpoint (/ -> 403)."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def make_settings(test_db: DbHarness, stub_http_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "EAIOS_ENV": "test",
        "DATABASE_URL": test_db.app_url,
        "DATABASE_POOL_SIZE": 2,
        "REDIS_URL": REDIS_URL,
        "QDRANT_URL": stub_http_url,
        "S3_ENDPOINT_URL": stub_http_url,
        "JWT_SECRET": JWT_SECRET,
        "RATE_LIMIT_KEY_PREFIX": f"eaios-test:{uuid.uuid4().hex}",
        "READINESS_TIMEOUT_SECONDS": 1.0,
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(test_db: DbHarness, stub_http_url: str) -> Settings:
    return make_settings(test_db, stub_http_url)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def redis_client() -> Iterator[Redis]:
    assert REDIS_URL
    client = Redis.from_url(REDIS_URL)
    yield client
    client.close()  # type: ignore[no-untyped-call]


def login(client: TestClient, test_db: DbHarness, ref: str) -> str:
    slug, key = ref.split("/", 1)
    response = client.post(
        "/api/v1/auth/login",
        json={
            "tenant": slug,
            "email": test_db.foundation.tenants[slug].users[key].email,
            "password": test_db.password(ref),
        },
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


def owner_exec(test_db: DbHarness, sql: str, params: dict[str, Any] | None = None) -> Any:
    with test_db.owner_engine.begin() as conn:
        result = conn.execute(text(sql), params or {})
        # Buffer rows before the connection closes: fetching afterwards reads freed
        # driver state (psycopg crashes when loading arrays such as rolconfig).
        return result.freeze()() if result.returns_rows else result


@contextmanager
def tenant_packs(test_db: DbHarness, slug: str, packs: list[str]) -> Iterator[None]:
    """Temporarily switch a fixture tenant's enabled packs (M4 tenant configuration)."""

    tenant_id = test_db.foundation.tenants[slug].id
    before = owner_exec(
        test_db, "SELECT enabled_packs FROM tenant_settings WHERE tenant_id = :t", {"t": tenant_id}
    ).scalar_one()
    sql = "UPDATE tenant_settings SET enabled_packs = :p WHERE tenant_id = :t"
    owner_exec(test_db, sql, {"p": packs, "t": tenant_id})
    try:
        yield
    finally:
        owner_exec(test_db, sql, {"p": list(before), "t": tenant_id})


# --- M2 report fixtures ---------------------------------------------------------------


@pytest.fixture(scope="session")
def report_db() -> Iterator[DbHarness]:
    """A separate disposable database with M0 fixtures and the NileTech sales import.

    Report tests change group memberships and grants to exercise revocation, so they
    do not share the session database used by the M0/M1 suites.
    """

    from app.business_data.loader import import_sales_dataset
    from app.business_data.model import normalize
    from app.business_data.source import verify_vendored

    with disposable_database(seed=True) as db:
        source = verify_vendored()
        import_sales_dataset(db.owner_engine, source, normalize(source), environment="test")
        yield db


def report_settings(db: DbHarness, stub_http_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "REPORT_DATABASE_URL": db.report_url,
        "REPORT_DISPATCH": "inline",
        "MODEL_MODE": "fake",
        "SSE_POLL_INTERVAL_SECONDS": 0.05,
        "SSE_HEARTBEAT_SECONDS": 0.5,
        "SSE_MAX_STREAM_SECONDS": 60,
    }
    values.update(overrides)
    return make_settings(db, stub_http_url, **values)


# --- M3 document fixtures -------------------------------------------------------------

QDRANT_URL = os.environ.get("TEST_QDRANT_URL")


@pytest.fixture(scope="session")
def qdrant_url() -> str:
    if not QDRANT_URL:
        message = "TEST_QDRANT_URL is required for document integration tests"
        if os.environ.get("EAIOS_REQUIRE_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return QDRANT_URL


@pytest.fixture(scope="session")
def documents_db() -> Iterator[DbHarness]:
    """Separate disposable database for document tests (they mutate permissions)."""

    with disposable_database(seed=True) as db:
        yield db


def document_settings(
    db: DbHarness, stub_http_url: str, qdrant: str, fs_root: str, prefix: str, **overrides: Any
) -> Settings:
    values: dict[str, Any] = {
        "QDRANT_URL": qdrant,
        "DOCUMENT_STORE": "filesystem",
        "DOCUMENT_FS_ROOT": fs_root,
        "QDRANT_COLLECTION_PREFIX": prefix,
        "REPORT_DISPATCH": "inline",
        "MODEL_MODE": "fake",
        "SSE_POLL_INTERVAL_SECONDS": 0.02,
        "SSE_HEARTBEAT_SECONDS": 0.5,
        "SSE_MAX_STREAM_SECONDS": 60,
    }
    values.update(overrides)
    return make_settings(db, stub_http_url, **values)
