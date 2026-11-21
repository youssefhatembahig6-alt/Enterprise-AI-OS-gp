"""Google Drive API v3 client used by the sync (ADR 0401).

Credentials are server-side only (NFR-05) and are bound to a tenant by the operator,
not by tenant administrators: ``DRIVE_CONNECTOR_CONFIG`` names a JSON file

    {"tenants": {"<tenant-slug>": {
        "auth": {"type": "service_account", "key_file": "/run/secrets/drive-sa.json"}
              | {"type": "oauth_refresh_token", "client_id": "...", "client_secret": "...",
                 "refresh_token": "..."},
        "allowed_roots": ["<folder id>", ...],
        "group_membership": "unavailable" | "directory"}}}

A tenant administrator can only add a source whose root folder the operator listed
for that tenant, so one tenant can never point a source at another tenant's folder.
The file is re-read on every use: removing a root takes effect at the next sync,
which then fails before requesting that folder from Drive.

Google group membership is only trusted when the operator enables
``group_membership: "directory"`` (Workspace Admin SDK, read-only member scope) and
the members can be listed. Otherwise ``list_group_members`` returns ``None``
("cannot verify") and the sync leaves that Drive group permission unresolved.

The client never guesses: any failure to list a folder raises ``DriveUnavailable`` /
``DriveAuthError`` (the sync then fails and the source's content is denied), and a
file whose permission list cannot be read raises ``PermissionsUnavailable`` (that
item is denied).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt

API = "https://www.googleapis.com/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, not a secret
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
DIRECTORY_API = "https://admin.googleapis.com/admin/directory/v1"
FOLDER = "application/vnd.google-apps.folder"
MAX_BYTES = 10 * 1024 * 1024
# Google-native types are exported; everything else is downloaded as stored.
EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}
READ_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})
_FILE_FIELDS = "id,name,mimeType,parents,md5Checksum,modifiedTime,size,trashed"


class DriveError(Exception):
    code = "drive_error"


class DriveNotConfigured(DriveError):
    code = "drive_not_configured"


class DriveAuthError(DriveError):
    code = "drive_auth_failed"


class DriveUnavailable(DriveError):
    code = "drive_unavailable"


class RootNotAllowed(DriveError):
    code = "root_not_allowed"


class RootNotAccessible(DriveError):
    code = "drive_root_not_accessible"


class PermissionsUnavailable(DriveError):
    code = "drive_permissions_unavailable"


class ContentUnavailable(DriveError):
    code = "drive_content_unavailable"


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    md5: str | None = None
    modified_time: str = ""
    size: int | None = None
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @property
    def content_fingerprint(self) -> str:
        # md5 for stored files; Google-native files have none, and modifiedTime only
        # changes with content edits (sharing changes do not touch it).
        return f"md5:{self.md5}" if self.md5 else f"mtime:{self.modified_time}"


@dataclass(frozen=True, slots=True)
class DrivePermission:
    type: str  # user | group | domain | anyone
    role: str
    email: str | None = None
    domain: str | None = None
    deleted: bool = False


class DriveClient(Protocol):
    def get_file(self, file_id: str) -> DriveFile: ...

    def list_children(self, folder_id: str) -> list[DriveFile]: ...

    def list_permissions(self, file_id: str) -> list[DrivePermission]: ...

    def download(self, file: DriveFile) -> tuple[bytes, str]: ...

    def list_group_members(self, group_email: str) -> frozenset[str] | None:
        """Current member emails, or ``None`` when membership cannot be verified."""
        ...


def export_name(file: DriveFile) -> str:
    export = EXPORTS.get(file.mime_type)
    if export and not file.name.lower().endswith(export[1]):
        return file.name + export[1]
    return file.name


def supported_type(file: DriveFile) -> bool:
    return not file.mime_type.startswith("application/vnd.google-apps.") or (
        file.mime_type in EXPORTS
    )


# -- configuration ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TenantDriveConfig:
    auth: dict[str, Any]
    allowed_roots: frozenset[str] = field(default_factory=frozenset)
    group_membership: str = "unavailable"  # or "directory" (Workspace only)


def load_connector_config(path: str | None) -> dict[str, TenantDriveConfig]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, TenantDriveConfig] = {}
    for slug, entry in (raw.get("tenants") or {}).items():
        auth = dict(entry.get("auth") or {})
        if auth.get("type") not in ("service_account", "oauth_refresh_token"):
            raise ValueError(f"drive connector config for {slug}: unknown auth type")
        membership = str(entry.get("group_membership") or "unavailable")
        if membership not in ("unavailable", "directory"):
            raise ValueError(f"drive connector config for {slug}: unknown group_membership")
        out[str(slug)] = TenantDriveConfig(
            auth=auth,
            allowed_roots=frozenset(str(r) for r in entry.get("allowed_roots") or []),
            group_membership=membership,
        )
    return out


class ConnectorConfig:
    """The operator file, re-read on every call (it is small and rarely changes), so a
    removed root or credential applies to the next sync without a restart."""

    def __init__(self, path: str | None) -> None:
        self._path = path

    def tenants(self) -> dict[str, TenantDriveConfig]:
        return load_connector_config(self._path)

    def tenant(self, slug: str) -> TenantDriveConfig | None:
        return self.tenants().get(slug)

    def allowed_roots(self, slug: str) -> frozenset[str]:
        entry = self.tenant(slug)
        return entry.allowed_roots if entry else frozenset()


# -- HTTP implementation -------------------------------------------------------------------


class _TokenSource:
    def __init__(self, auth: dict[str, Any], http: httpx.Client, scope: str = SCOPE) -> None:
        self._scope = scope
        self._auth = auth
        self._http = http
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    def _request(self) -> dict[str, Any]:
        if self._auth["type"] == "oauth_refresh_token":
            data = {
                "grant_type": "refresh_token",
                "client_id": self._auth["client_id"],
                "client_secret": self._auth["client_secret"],
                "refresh_token": self._auth["refresh_token"],
            }
        else:
            key = json.loads(Path(self._auth["key_file"]).read_text(encoding="utf-8"))
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": key["client_email"],
                    "scope": self._scope,
                    "aud": TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                key["private_key"],
                algorithm="RS256",
                headers={"kid": key.get("private_key_id")},
            )
            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as exc:
            raise DriveUnavailable(str(type(exc).__name__)) from exc
        if response.status_code in (400, 401, 403):
            raise DriveAuthError(f"token endpoint {response.status_code}")
        if response.status_code != 200:
            raise DriveUnavailable(f"token endpoint {response.status_code}")
        body: dict[str, Any] = response.json()
        return body

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires - 60:
                body = self._request()
                self._token = str(body["access_token"])
                self._expires = time.time() + float(body.get("expires_in", 3600))
            return self._token


def _file(raw: dict[str, Any]) -> DriveFile:
    return DriveFile(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        mime_type=str(raw.get("mimeType") or "application/octet-stream"),
        parents=tuple(raw.get("parents") or ()),
        md5=raw.get("md5Checksum"),
        modified_time=str(raw.get("modifiedTime") or ""),
        size=int(raw["size"]) if raw.get("size") is not None else None,
        trashed=bool(raw.get("trashed", False)),
    )


class HttpDriveClient:
    def __init__(
        self,
        auth: dict[str, Any],
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        group_membership: str = "unavailable",
    ) -> None:
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._group_membership = group_membership
        scope = f"{SCOPE} {DIRECTORY_SCOPE}" if group_membership == "directory" else SCOPE
        self._tokens = _TokenSource(auth, self._http, scope)

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            return self._http.get(
                f"{API}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
            )
        except httpx.HTTPError as exc:
            raise DriveUnavailable(type(exc).__name__) from exc

    @staticmethod
    def _check(response: httpx.Response, not_found: type[DriveError]) -> dict[str, Any]:
        if response.status_code == 401:
            raise DriveAuthError("401")
        if response.status_code in (403, 404):
            raise not_found(str(response.status_code))
        if response.status_code != 200:
            raise DriveUnavailable(str(response.status_code))
        body: dict[str, Any] = response.json()
        return body

    def get_file(self, file_id: str) -> DriveFile:
        body = self._check(
            self._get(f"/files/{file_id}", {"fields": _FILE_FIELDS, "supportsAllDrives": "true"}),
            RootNotAccessible,
        )
        return _file(body)

    def list_children(self, folder_id: str) -> list[DriveFile]:
        out: list[DriveFile] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            # A folder that cannot be listed fails the whole sync (never a partial view).
            body = self._check(self._get("/files", params), DriveUnavailable)
            out.extend(_file(f) for f in body.get("files", []))
            token = body.get("nextPageToken")
            if not token:
                return out

    def list_permissions(self, file_id: str) -> list[DrivePermission]:
        out: list[DrivePermission] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "fields": "nextPageToken,permissions(type,role,emailAddress,domain,deleted)",
                "supportsAllDrives": "true",
                "pageSize": 100,
            }
            if token:
                params["pageToken"] = token
            body = self._check(
                self._get(f"/files/{file_id}/permissions", params), PermissionsUnavailable
            )
            if "permissions" not in body:
                raise PermissionsUnavailable("no permission list returned")
            out.extend(
                DrivePermission(
                    type=str(p.get("type", "")),
                    role=str(p.get("role", "")),
                    email=(p.get("emailAddress") or None),
                    domain=p.get("domain"),
                    deleted=bool(p.get("deleted", False)),
                )
                for p in body["permissions"]
            )
            token = body.get("nextPageToken")
            if not token:
                return out

    def download(self, file: DriveFile) -> tuple[bytes, str]:
        export = EXPORTS.get(file.mime_type)
        if export:
            response = self._get(f"/files/{file.id}/export", {"mimeType": export[0]})
        else:
            if file.size is not None and file.size > MAX_BYTES:
                raise ContentUnavailable("too_large")
            response = self._get(f"/files/{file.id}", {"alt": "media", "supportsAllDrives": "true"})
        if response.status_code == 401:
            raise DriveAuthError("401")
        if response.status_code != 200:
            raise ContentUnavailable(str(response.status_code))
        if len(response.content) > MAX_BYTES:
            raise ContentUnavailable("too_large")
        return response.content, export_name(file)

    def list_group_members(self, group_email: str) -> frozenset[str] | None:
        """Direct members through the Workspace Directory API. Any doubt — not enabled,
        an error, a nested group or a member without an email — returns ``None``."""

        if self._group_membership != "directory":
            return None
        members: set[str] = set()
        token: str | None = None
        try:
            while True:
                params: dict[str, Any] = {"maxResults": 200}
                if token:
                    params["pageToken"] = token
                response = self._http.get(
                    f"{DIRECTORY_API}/groups/{group_email}/members",
                    params=params,
                    headers={"Authorization": f"Bearer {self._tokens.token()}"},
                )
                if response.status_code != 200:
                    return None
                body = response.json()
                for m in body.get("members", []):
                    if m.get("type") != "USER" or not m.get("email"):
                        return None  # nested groups or customers cannot be verified here
                    if m.get("status", "ACTIVE") == "ACTIVE":
                        members.add(str(m["email"]).lower())
                token = body.get("nextPageToken")
                if not token:
                    return frozenset(members)
        except (httpx.HTTPError, DriveError, ValueError):
            return None


DriveClientFactory = Callable[[str], DriveClient | None]


def http_client_factory(config: ConnectorConfig) -> DriveClientFactory:
    """Per-tenant client from the operator configuration (``None``: not configured)."""

    def build(tenant_slug: str) -> DriveClient | None:
        entry = config.tenant(tenant_slug)
        if entry is None:
            return None
        return HttpDriveClient(entry.auth, group_membership=entry.group_membership)

    return build
