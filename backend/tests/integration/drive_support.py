"""In-memory Google Drive double for M4 sync tests.

This is a **test double**: it exercises the sync against real PostgreSQL, Qdrant and
the M3 ingestion path, but it is not evidence that the live Drive API behaves the same
way. The live acceptance run uses ``HttpDriveClient`` (docs/evidence/m4/README.md).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any

from app.connectors.drive import (
    FOLDER,
    ContentUnavailable,
    DriveFile,
    DrivePermission,
    DriveUnavailable,
    PermissionsUnavailable,
    RootNotAccessible,
)


@dataclass
class Node:
    id: str
    name: str
    mime: str
    parent: str | None
    content: bytes = b""
    perms: list[DrivePermission] = field(default_factory=list)
    trashed: bool = False
    modified: int = 1


class FakeDrive:
    def __init__(self, root_id: str = "rootFolder001") -> None:
        self.root_id = root_id
        self.nodes: dict[str, Node] = {root_id: Node(root_id, "root", FOLDER, None)}
        self.fail_listing: set[str] = set()
        self.unreadable_permissions: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        # Google group email -> verified member emails; absent = membership unknown.
        self.group_members: dict[str, set[str]] = {}
        # (method, id) -> callback run once, after the call returns and outside the lock,
        # so tests can change state deterministically in the middle of a sync.
        self.hooks: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._counter = 0

    # -- test controls --------------------------------------------------------------------

    def _id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:06d}xyz"

    def folder(self, name: str, parent: str | None = None) -> str:
        fid = self._id("fold")
        self.nodes[fid] = Node(fid, name, FOLDER, parent or self.root_id)
        return fid

    def file(
        self,
        name: str,
        content: str | bytes,
        *,
        parent: str | None = None,
        users: tuple[str, ...] = (),
        groups: tuple[str, ...] = (),
        mime: str = "text/markdown",
        extra: tuple[DrivePermission, ...] = (),
    ) -> str:
        fid = self._id("file")
        data = content.encode() if isinstance(content, str) else content
        perms = [DrivePermission("user", "owner", "owner@drive.example")]
        perms += [DrivePermission("user", "reader", e) for e in users]
        perms += [DrivePermission("group", "reader", g) for g in groups]
        perms += list(extra)
        self.nodes[fid] = Node(fid, name, mime, parent or self.root_id, data, perms)
        return fid

    def share(self, file_id: str, email: str, kind: str = "user", role: str = "reader") -> None:
        self.nodes[file_id].perms.append(DrivePermission(kind, role, email))

    def unshare(self, file_id: str, email: str) -> None:
        node = self.nodes[file_id]
        node.perms = [p for p in node.perms if (p.email or "").lower() != email.lower()]

    def edit(self, file_id: str, content: str) -> None:
        node = self.nodes[file_id]
        node.content = content.encode()
        node.modified += 1

    def move(self, file_id: str, parent: str) -> None:
        self.nodes[file_id].parent = parent

    def rename(self, file_id: str, name: str) -> None:
        self.nodes[file_id].name = name

    def trash(self, file_id: str) -> None:
        self.nodes[file_id].trashed = True

    def delete(self, file_id: str) -> None:
        self.nodes.pop(file_id)

    # -- DriveClient -----------------------------------------------------------------------

    def _hook(self, method: str, key: str) -> None:
        hook = self.hooks.pop((method, key), None)
        if hook is not None:
            hook()

    def _drive_file(self, n: Node) -> DriveFile:
        md5 = (
            None
            if n.mime.startswith("application/vnd.google-apps")
            else hashlib.md5(  # noqa: S324
                n.content
            ).hexdigest()
        )
        return DriveFile(
            id=n.id,
            name=n.name,
            mime_type=n.mime,
            parents=(n.parent,) if n.parent else (),
            md5=md5,
            modified_time=f"2026-11-21T00:00:{n.modified:02d}Z",
            size=len(n.content),
            trashed=n.trashed,
        )

    def get_file(self, file_id: str) -> DriveFile:
        with self._lock:
            self.calls.append(("get_file", file_id))
            node = self.nodes.get(file_id)
            if node is None or file_id in self.fail_listing:
                raise RootNotAccessible("404")
            return self._drive_file(node)

    def list_children(self, folder_id: str) -> list[DriveFile]:
        with self._lock:
            self.calls.append(("list_children", folder_id))
            if folder_id in self.fail_listing:
                raise DriveUnavailable("500")
            children = [
                self._drive_file(n)
                for n in self.nodes.values()
                if n.parent == folder_id and not n.trashed
            ]
        self._hook("list_children", folder_id)
        return children

    def list_permissions(self, file_id: str) -> list[DrivePermission]:
        with self._lock:
            self.calls.append(("list_permissions", file_id))
            if file_id in self.unreadable_permissions:
                raise PermissionsUnavailable("403")
            perms = list(self.nodes[file_id].perms)
        self._hook("list_permissions", file_id)
        return perms

    def list_group_members(self, group_email: str) -> frozenset[str] | None:
        with self._lock:
            self.calls.append(("list_group_members", group_email))
            members = self.group_members.get(group_email.lower())
            return frozenset(m.lower() for m in members) if members is not None else None

    def download(self, file: DriveFile) -> tuple[bytes, str]:
        with self._lock:
            self.calls.append(("download", file.id))
            node = self.nodes.get(file.id)
            if node is None:
                raise ContentUnavailable("404")
            return node.content, file.name

    def requested(self, file_id: str) -> bool:
        return any(c[1] == file_id for c in self.calls)

    def downloads(self) -> int:
        return sum(1 for c in self.calls if c[0] == "download")


class FakeDriveFactory:
    """Per-tenant-slug fakes; a slug without a fake is "not configured"."""

    def __init__(self) -> None:
        self.drives: dict[str, FakeDrive] = {}

    def __call__(self, slug: str) -> Any:
        return self.drives.get(slug)
