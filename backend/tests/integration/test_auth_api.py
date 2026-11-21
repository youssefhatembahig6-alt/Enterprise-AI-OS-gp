"""Login, identity, token validation, revocation and tampering via the HTTP API."""

from __future__ import annotations

import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db.engine import tenant_transaction
from app.logging_setup import JsonFormatter
from app.main import create_app
from tests.integration.conftest import (
    JWT_SECRET,
    DbHarness,
    login,
    make_settings,
    owner_exec,
)

ISS, AUD = "eaios-api", "eaios-web"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_login_and_identity_are_server_derived(client: TestClient, test_db: DbHarness) -> None:
    token = login(client, test_db, "niletech/finance-reader")
    response = client.get("/api/v1/me", headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    tenant = test_db.foundation.tenants["niletech"]
    user = tenant.users["finance-reader"]
    assert body["user_id"] == str(user.id)
    assert body["tenant"] == {"id": str(tenant.id), "slug": "niletech", "name": tenant.name}
    assert body["roles"] == ["reader", "report_reader"]
    assert body["capabilities"] == [
        {"capability": "dataset.query", "scope_department": None},
        {"capability": "resource.read", "scope_department": None},
    ]
    assert body["groups"] == ["all-staff", "finance-team"]
    assert body["attributes"] == {
        "clearance": "confidential",
        "country": "EG",
        "department": "finance",
    }
    assert body["policy_epoch"] >= 1
    assert "password" not in response.text
    assert response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store"


def test_scoped_admin_identity(client: TestClient, test_db: DbHarness) -> None:
    token = login(client, test_db, "niletech/finance-admin")
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["capabilities"] == [
        {"capability": "audit.read", "scope_department": "finance"},
        {"capability": "identity.admin", "scope_department": "finance"},
        {"capability": "resource.admin", "scope_department": "finance"},
    ]


def test_second_tenant_user_sees_only_own_tenant(client: TestClient, test_db: DbHarness) -> None:
    token = login(client, test_db, "isolation-test/finance-reader")
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["tenant"]["slug"] == "isolation-test"
    assert "niletech" not in client.get("/api/v1/me", headers=auth(token)).text.lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant": "niletech", "email": "finance.reader@niletech.example", "password": "wrong"},
        {"tenant": "niletech", "email": "nobody@niletech.example", "password": "wrong"},
        {"tenant": "no-such-tenant", "email": "finance.reader@niletech.example", "password": "x"},
    ],
)
def test_failed_login_is_generic(client: TestClient, payload: dict[str, str]) -> None:
    response = client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_credentials"
    assert error["message"] == "Invalid sign-in details."
    assert payload["password"] not in response.text


def test_correct_password_wrong_tenant_fails(client: TestClient, test_db: DbHarness) -> None:
    user = test_db.foundation.tenants["niletech"].users["finance-reader"]
    response = client.post(
        "/api/v1/auth/login",
        json={
            "tenant": "isolation-test",
            "email": user.email,
            "password": test_db.password("niletech/finance-reader"),
        },
    )
    assert response.status_code == 401


def test_inactive_user_cannot_log_in(client: TestClient, test_db: DbHarness) -> None:
    user = test_db.foundation.tenants["niletech"].users["inactive-reader"]
    response = client.post(
        "/api/v1/auth/login",
        json={
            "tenant": "niletech",
            "email": user.email,
            "password": test_db.password("niletech/inactive-reader"),
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_rejects_extra_privilege_fields(client: TestClient, test_db: DbHarness) -> None:
    user = test_db.foundation.tenants["niletech"].users["sales-reader"]
    response = client.post(
        "/api/v1/auth/login",
        json={
            "tenant": "niletech",
            "email": user.email,
            "password": test_db.password("niletech/sales-reader"),
            "role": "access_admin",
            "clearance": "restricted",
            "groups": ["finance-team"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert test_db.password("niletech/sales-reader") not in response.text


def test_failed_login_rate_limited(client: TestClient, test_db: DbHarness) -> None:
    email = test_db.foundation.tenants["isolation-test"].users["sales-reader"].email
    payload = {"tenant": "isolation-test", "email": email, "password": "wrong-password"}
    statuses = [client.post("/api/v1/auth/login", json=payload).status_code for _ in range(5)]
    assert statuses == [401] * 5
    limited = client.post("/api/v1/auth/login", json=payload)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0
    # Even the correct password is refused while limited.
    payload["password"] = test_db.password("isolation-test/sales-reader")
    assert client.post("/api/v1/auth/login", json=payload).status_code == 429


def test_rate_limit_fails_closed_without_redis(test_db: DbHarness, stub_http_url: str) -> None:
    settings = make_settings(test_db, stub_http_url, REDIS_URL="redis://127.0.0.1:1/0")
    with TestClient(create_app(settings)) as client:
        user = test_db.foundation.tenants["niletech"].users["sales-reader"]
        response = client.post(
            "/api/v1/auth/login",
            json={
                "tenant": "niletech",
                "email": user.email,
                "password": test_db.password("niletech/sales-reader"),
            },
        )
    assert response.status_code == 503
    assert "redis" not in response.text.lower()


def test_missing_or_malformed_authorization(client: TestClient) -> None:
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not-a-jwt"}):
        response = client.get("/api/v1/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.headers["WWW-Authenticate"] == "Bearer"


def _forge(
    test_db: DbHarness, ref: str, secret: str = JWT_SECRET, alg: str = "HS256", **overrides: object
) -> str:
    slug, key = ref.split("/", 1)
    tenant = test_db.foundation.tenants[slug]
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(tenant.users[key].id),
        "tenant_id": str(tenant.id),
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": ISS,
        "aud": AUD,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, secret, algorithm=alg)


def _decoded(token: str) -> dict[str, object]:
    return dict(jwt.decode(token, options={"verify_signature": False}))


@pytest.mark.parametrize(
    "variant",
    [
        "bad_signature",
        "alg_none",
        "alg_hs512",
        "expired",
        "wrong_issuer",
        "wrong_audience",
        "missing_exp",
        "missing_sub",
        "missing_tenant",
        "missing_jti",
        "unknown_session",
    ],
)
def test_invalid_tokens_rejected(client: TestClient, test_db: DbHarness, variant: str) -> None:
    real = _decoded(login(client, test_db, "niletech/finance-reader"))
    ref = "niletech/finance-reader"
    jti = str(real["jti"])
    past = int((datetime.now(UTC) - timedelta(seconds=10)).timestamp())
    tokens = {
        "bad_signature": lambda: _forge(test_db, ref, secret="x" * 48, jti=jti),
        "alg_none": lambda: jwt.encode({**real}, cast(Any, None), algorithm="none"),
        "alg_hs512": lambda: _forge(test_db, ref, alg="HS512", jti=jti),
        "expired": lambda: _forge(test_db, ref, exp=past, jti=jti),
        "wrong_issuer": lambda: _forge(test_db, ref, iss="evil", jti=jti),
        "wrong_audience": lambda: _forge(test_db, ref, aud="evil", jti=jti),
        "missing_exp": lambda: _forge(test_db, ref, exp=None, jti=jti),
        "missing_sub": lambda: _forge(test_db, ref, sub=None, jti=jti),
        "missing_tenant": lambda: _forge(test_db, ref, tenant_id=None, jti=jti),
        "missing_jti": lambda: _forge(test_db, ref, jti=None),
        # Correctly signed but not backed by a server-side session.
        "unknown_session": lambda: _forge(test_db, ref),
    }
    response = client.get("/api/v1/me", headers=auth(tokens[variant]()))
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "unauthenticated",
        "message": "Authentication required.",
        "request_id": response.headers["X-Request-ID"],
    }


def test_tampered_tenant_claim_cannot_reach_other_tenant(
    client: TestClient, test_db: DbHarness
) -> None:
    """Even with the signing key, swapping tenant_id/sub cannot reuse another tenant's session."""

    real = _decoded(login(client, test_db, "niletech/finance-reader"))
    other = test_db.foundation.tenants["isolation-test"]
    swapped_tenant = _forge(
        test_db, "niletech/finance-reader", tenant_id=str(other.id), jti=str(real["jti"])
    )
    swapped_user = _forge(
        test_db,
        "niletech/finance-reader",
        tenant_id=str(other.id),
        sub=str(other.users["finance-reader"].id),
        jti=str(real["jti"]),
    )
    for token in (swapped_tenant, swapped_user):
        assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_request_headers_and_query_cannot_override_context(
    client: TestClient, test_db: DbHarness
) -> None:
    token = login(client, test_db, "niletech/sales-reader")
    other = test_db.foundation.tenants["isolation-test"]
    response = client.get(
        "/api/v1/me",
        params={"tenant_id": str(other.id), "role": "access_admin", "clearance": "restricted"},
        headers={
            **auth(token),
            "X-Tenant-Id": str(other.id),
            "X-Role": "access_admin",
            "X-User-Country": "EG",
            "X-Groups": "finance-team",
            "X-Clearance": "restricted",
            "X-Request-ID": "attacker-chosen",
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["tenant"]["slug"] == "niletech"
    assert body["roles"] == ["reader"]
    assert body["groups"] == ["all-staff"]
    assert body["attributes"] == {"clearance": "internal", "country": "AE", "department": "sales"}
    assert response.headers["X-Request-ID"] != "attacker-chosen"


def test_logout_revokes_session(client: TestClient, test_db: DbHarness) -> None:
    token = login(client, test_db, "niletech/finance-reader")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 204
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 401


def test_logout_revokes_only_that_session(client: TestClient, test_db: DbHarness) -> None:
    first = login(client, test_db, "niletech/board-member")
    second = login(client, test_db, "niletech/board-member")
    client.post("/api/v1/auth/logout", headers=auth(first))
    assert client.get("/api/v1/me", headers=auth(second)).status_code == 200


def test_deactivation_rejects_existing_token(
    client: TestClient, test_db: DbHarness, app_engine: Engine
) -> None:
    tenant = test_db.foundation.tenants["isolation-test"]
    user = tenant.users["denied-reader"]
    token = login(client, test_db, "isolation-test/denied-reader")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200
    with tenant_transaction(app_engine, tenant.id) as conn:
        conn.execute(text("UPDATE users SET status = 'inactive' WHERE id = :u"), {"u": user.id})
    try:
        assert client.get("/api/v1/me", headers=auth(token)).status_code == 401
    finally:
        with tenant_transaction(app_engine, tenant.id) as conn:
            conn.execute(text("UPDATE users SET status = 'active' WHERE id = :u"), {"u": user.id})


def test_server_side_expiry_enforced(client: TestClient, test_db: DbHarness) -> None:
    token = login(client, test_db, "isolation-test/finance-admin")
    jti = _decoded(token)["jti"]
    owner_exec(
        test_db,
        "UPDATE sessions SET issued_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 second' WHERE id = :id",
        {"id": jti},
    )
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_sessions_are_tenant_isolated(
    client: TestClient, test_db: DbHarness, app_engine: Engine
) -> None:
    login(client, test_db, "niletech/finance-reader")
    login(client, test_db, "isolation-test/finance-reader")
    for slug in ("niletech", "isolation-test"):
        tenant_id = test_db.foundation.tenants[slug].id
        with tenant_transaction(app_engine, tenant_id) as conn:
            seen = set(conn.execute(text("SELECT DISTINCT tenant_id FROM sessions")).scalars())
        assert seen == {tenant_id}


def test_cors_allows_only_configured_origin(client: TestClient) -> None:
    allowed = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    denied = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_audit_events_and_no_secret_leakage(
    client: TestClient,
    test_db: DbHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    stream = io.StringIO()
    json_handler = logging.StreamHandler(stream)
    json_handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(json_handler)
    tenant = test_db.foundation.tenants["niletech"]
    user = tenant.users["denied-reader"]
    password = test_db.password("niletech/denied-reader")
    started = datetime.now(UTC)

    bad = client.post(
        "/api/v1/auth/login",
        json={"tenant": "niletech", "email": user.email, "password": password + "-wrong"},
    )
    good = client.post(
        "/api/v1/auth/login", json={"tenant": "niletech", "email": user.email, "password": password}
    )
    token = good.json()["access_token"]
    me = client.get("/api/v1/me", headers=auth(token))
    forged = client.get("/api/v1/me", headers=auth(_forge(test_db, "niletech/denied-reader")))
    out = client.post("/api/v1/auth/logout", headers=auth(token))
    assert (
        bad.status_code,
        good.status_code,
        me.status_code,
        forged.status_code,
        out.status_code,
    ) == (401, 200, 200, 401, 204)

    rows = owner_exec(
        test_db,
        "SELECT event_type, action, decision, reason, tenant_id, user_id, request_id, details"
        " FROM audit_events WHERE occurred_at >= :t AND (user_id = :u OR user_id IS NULL)"
        " ORDER BY occurred_at",
        {"t": started, "u": user.id},
    ).all()
    seen = {(r.event_type, r.decision, r.reason) for r in rows}
    assert ("auth.login", "deny", "bad_password") in seen
    assert ("auth.login", "allow", "password_verified") in seen
    assert ("auth.token", "deny", "session_not_found") in seen
    assert ("auth.logout", "info", "session_revoked") in seen
    assert all(r.request_id for r in rows)
    assert any(r.tenant_id == tenant.id and r.user_id == user.id for r in rows)

    audit_text = " ".join(str(tuple(r)) for r in rows)
    logging.getLogger().removeHandler(json_handler)
    logs = caplog.text + stream.getvalue()
    assert "audit_event" in logs
    responses = bad.text + good.text.replace(token, "") + me.text + forged.text
    for secret in [*test_db.secrets, password + "-wrong", token]:
        assert secret not in audit_text
        assert secret not in logs
    for secret in [*test_db.secrets, password + "-wrong"]:
        assert secret not in responses
