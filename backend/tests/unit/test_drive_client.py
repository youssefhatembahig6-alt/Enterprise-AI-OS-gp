"""HttpDriveClient against a mocked Drive API (httpx.MockTransport): request shapes,
pagination, exports and the fail-closed error mapping. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.connectors.drive import (
    TOKEN_URL,
    DriveAuthError,
    DriveFile,
    DriveUnavailable,
    HttpDriveClient,
    PermissionsUnavailable,
    RootNotAccessible,
    load_connector_config,
)

OAUTH = {
    "type": "oauth_refresh_token",
    "client_id": "c",
    "client_secret": "s",
    "refresh_token": "r",
}


def client(handler: Any, auth: dict[str, Any] | None = None) -> HttpDriveClient:
    def route(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        result: httpx.Response = handler(request)
        return result

    return HttpDriveClient(auth or OAUTH, transport=httpx.MockTransport(route))


def test_children_are_paginated_and_scoped_to_the_folder() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        if "pageToken" not in params:
            return httpx.Response(
                200,
                json={
                    "files": [{"id": "a1aaaa", "name": "a.md", "mimeType": "text/markdown"}],
                    "nextPageToken": "p2",
                },
            )
        return httpx.Response(200, json={"files": [{"id": "b2bbbb", "name": "b.md"}]})

    files = client(handler).list_children("folder1")
    assert [f.id for f in files] == ["a1aaaa", "b2bbbb"]
    assert seen[0]["q"] == "'folder1' in parents and trashed = false"
    assert seen[1]["pageToken"] == "p2"


@pytest.mark.parametrize("status", [403, 404])
def test_unreadable_permissions_raise(status: int) -> None:
    c = client(lambda r: httpx.Response(status, json={}))
    with pytest.raises(PermissionsUnavailable):
        c.list_permissions("file01")


def test_missing_permission_list_is_not_an_empty_list() -> None:
    c = client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(PermissionsUnavailable):
        c.list_permissions("file01")


def test_permissions_are_parsed() -> None:
    body = {
        "permissions": [
            {"type": "user", "role": "reader", "emailAddress": "A@x.example"},
            {"type": "anyone", "role": "reader"},
            {"type": "user", "role": "writer", "emailAddress": "gone@x.example", "deleted": True},
        ]
    }
    perms = client(lambda r: httpx.Response(200, json=body)).list_permissions("file01")
    assert [(p.type, p.email, p.deleted) for p in perms] == [
        ("user", "A@x.example", False),
        ("anyone", None, False),
        ("user", "gone@x.example", True),
    ]


def test_listing_errors_fail_closed() -> None:
    with pytest.raises(DriveUnavailable):
        client(lambda r: httpx.Response(500)).list_children("folder1")
    with pytest.raises(DriveUnavailable):
        client(lambda r: httpx.Response(403)).list_children("folder1")
    with pytest.raises(RootNotAccessible):
        client(lambda r: httpx.Response(404)).get_file("folder1")
    with pytest.raises(DriveAuthError):
        client(lambda r: httpx.Response(401)).get_file("folder1")


def test_rejected_refresh_token_is_an_auth_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(DriveAuthError):
        HttpDriveClient(OAUTH, transport=transport).get_file("folder1")


def test_google_docs_are_exported_as_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/files/doc001/export")
        assert request.url.params["mimeType"] == "text/plain"
        return httpx.Response(200, content=b"Exported text")

    doc = DriveFile("doc001", "Policy", "application/vnd.google-apps.document")
    data, name = client(handler).download(doc)
    assert (data, name) == (b"Exported text", "Policy.txt")


def test_service_account_assertion_is_signed(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    key_file = tmp_path / "sa.json"
    key_file.write_text(
        json.dumps({"client_email": "sa@p.iam.example", "private_key": pem, "private_key_id": "k1"})
    )
    grants: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            grants.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(200, json={"id": "root01", "name": "r", "mimeType": "x"})

    sa = HttpDriveClient(
        {"type": "service_account", "key_file": str(key_file)}, transport=httpx.MockTransport(route)
    )
    sa.get_file("root01")
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in grants[0]


def test_connector_config_binds_tenants_to_roots(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"tenants": {"t1": {"auth": OAUTH, "allowed_roots": ["abcde"]}}}))
    config = load_connector_config(str(path))
    assert config["t1"].allowed_roots == frozenset({"abcde"})
    assert load_connector_config(None) == {}
    path.write_text(json.dumps({"tenants": {"t1": {"auth": {"type": "api_key"}}}}))
    with pytest.raises(ValueError, match="unknown auth type"):
        load_connector_config(str(path))


def directory_client(handler: Any) -> HttpDriveClient:
    def route(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        result: httpx.Response = handler(request)
        return result

    return HttpDriveClient(
        OAUTH, transport=httpx.MockTransport(route), group_membership="directory"
    )


def test_group_membership_is_unverified_unless_the_operator_enables_it() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no directory call without group_membership=directory")

    assert client(fail).list_group_members("team@x.example") is None


def test_group_membership_lists_active_user_members() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/groups/team@x.example/members")
        return httpx.Response(
            200,
            json={
                "members": [
                    {"type": "USER", "email": "A@x.example", "status": "ACTIVE"},
                    {"type": "USER", "email": "b@x.example", "status": "SUSPENDED"},
                ]
            },
        )

    assert directory_client(handler).list_group_members("team@x.example") == frozenset(
        {"a@x.example"}
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={}),  # consumer account / no Workspace directory
        httpx.Response(200, json={"members": [{"type": "GROUP", "email": "nested@x.example"}]}),
        httpx.Response(200, json={"members": [{"type": "USER"}]}),
    ],
)
def test_group_membership_doubt_is_none(response: httpx.Response) -> None:
    assert directory_client(lambda r: response).list_group_members("team@x.example") is None
