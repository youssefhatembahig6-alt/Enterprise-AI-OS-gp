"""Upload → parse → chunk → embed → publish, safe to retry (ADR 0301).

Ordering guarantees:

1. **Permissions first.** ``upload`` stores the bytes, then in one transaction creates
   the protected resource with its permissions (or ``permission_state = 'unknown'``),
   the document, version and the ingestion job. Only after that commit is the job
   dispatched.
2. **Deterministic identity.** A job owns one generation number (recorded on first
   claim). Point ids are ``uuid5(tenant | source | document | version | generation |
   ordinal)``, so a retry rewrites the same chunk rows and upserts the same Qdrant
   points: no duplicates. Identical bytes under another tenant or another document
   get different ids.
3. **No partial index.** Chunks and points are written under a *staging* generation,
   counted in PostgreSQL and Qdrant, and only then activated in one transaction that
   also refuses deleted or superseded documents. Search only ever uses the active
   generation's point ids, so a crash at any step leaves the previous generation
   searchable and never a mix.
4. **Tombstones.** Deleted documents keep ``deleted_at``; a late retry finishes its job
   without activating anything.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.config import Settings
from app.db.engine import tenant_transaction
from app.documents import access, store
from app.documents.canonical import CanonicalDocument, elements_to_json, from_stored
from app.documents.chunker import CHUNKER_VERSION, ChunkSpec, chunk_document
from app.documents.parsers import DoclingUnavailable, ParseFailed, detect, parse
from app.documents.qdrant import QdrantClient, QdrantError, SparseVector
from app.documents.storage import ObjectStore, StorageError, object_key, read_verified
from app.reporting.model_client import (
    GatewayNotConfigured,
    GatewayTimeout,
    ModelClient,
    ModelError,
)
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext
from app.tenancy.config import require_connector

logger = logging.getLogger("eaios.documents")
POINT_NAMESPACE = uuid.UUID("2b6f0c1e-7d4a-4f53-9a0e-5c1d2e3f4a5b")


class DocumentNotFound(Exception):
    """Unknown, foreign or not-visible document (uniform 404)."""


class DocumentForbidden(Exception):
    """Caller may see the document but may not administer it."""


class InvalidRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    """Sharing for an upload. ``grants`` are ``(effect, kind, key)`` with kind
    ``group`` (group key) or ``user`` (email); resolved inside the tenant."""

    classification: str
    department_restricted: bool
    allowed_countries: tuple[str, ...]
    grants: tuple[tuple[str, str, str], ...]


def point_id(
    tenant_id: UUID, source: str, document_id: UUID, version: int, generation: int, ordinal: int
) -> UUID:
    return uuid.uuid5(
        POINT_NAMESPACE, f"{tenant_id}|{source}|{document_id}|{version}|{generation}|{ordinal}"
    )


def collection_name(settings: Settings, model: ModelClient) -> str:
    return f"{settings.qdrant_collection_prefix}__{model.embedding_space().slug}"


def _title_from(filename: str) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename).strip() or "Untitled"
    return stem[:300]


# -- API-side service ----------------------------------------------------------------------


class DocumentService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        objects: ObjectStore,
        qdrant: QdrantClient,
        model: ModelClient,
        dispatch: Callable[[UUID, UUID], None] | None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._objects = objects
        self._qdrant = qdrant
        self._model = model
        self._dispatch = dispatch

    @property
    def available(self) -> bool:
        return self._dispatch is not None

    def _resolve(self, conn: Any, spec: PermissionSpec, department: str) -> store.Permissions:
        grants: list[tuple[str, UUID | None, UUID | None]] = []
        for effect, kind, key in spec.grants:
            if effect not in ("allow", "deny") or kind not in ("group", "user"):
                raise InvalidRequest("invalid_grant", "Grants need allow/deny and a group or user.")
            if kind == "group":
                gid = conn.execute(
                    text("SELECT id FROM groups WHERE key = :k"), {"k": key}
                ).scalar()
                if gid is None:
                    raise InvalidRequest(
                        "unknown_group", "A group in the sharing list does not exist."
                    )
                grants.append((effect, None, gid))
            else:
                uid = conn.execute(
                    text("SELECT id FROM users WHERE email = :e"), {"e": key.strip().lower()}
                ).scalar()
                if uid is None:
                    raise InvalidRequest(
                        "unknown_user", "A user in the sharing list does not exist."
                    )
                grants.append((effect, uid, None))
        return store.Permissions(
            classification=spec.classification,
            department=department,
            department_restricted=spec.department_restricted,
            allowed_countries=spec.allowed_countries,
            grants=tuple(grants),
        )

    def upload(
        self,
        ctx: AccessContext,
        *,
        filename: str,
        data: bytes,
        department: str,
        permissions: PermissionSpec | None,
        title: str | None = None,
        source: str = "upload",
    ) -> UUID:
        if not access.can_administer(ctx.subject, ctx.tenant_id, department).allowed:
            raise DocumentForbidden
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            require_connector(conn, "upload")  # M4: backend-enforced tenant configuration
        try:
            detected = detect(filename, data)
        except ParseFailed as exc:
            raise InvalidRequest(exc.code, exc.message) from exc
        if len(data) > 10 * 1024 * 1024:
            raise InvalidRequest("too_large", "The file is larger than 10 MB.")
        digest = hashlib.sha256(data).hexdigest()
        document_id = uuid.uuid4()
        key = object_key(ctx.tenant_id, source, document_id, 1, filename)
        self._objects.put(key, data)
        try:
            with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
                perms = self._resolve(conn, permissions, department) if permissions else None
                _, job_id, _ = store.create_document(
                    conn,
                    tenant_id=ctx.tenant_id,
                    title=(title or _title_from(filename))[:300],
                    filename=filename[:255],
                    media_type=detected.media_type,
                    byte_size=len(data),
                    byte_sha256=digest,
                    storage_key_for=lambda d, v: key,
                    permissions=perms,
                    unknown_department=department,
                    created_by=ctx.user_id,
                    source=source,
                    document_id=document_id,
                )
                record_audit_event(
                    conn,
                    request_id=ctx.request_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    event_type="document.upload",
                    action="resource.admin",
                    resource_id=document_id,
                    decision="allow",
                    reason="uploaded",
                    details={
                        "version": 1,
                        "byte_sha256": digest,
                        "permission_state": "verified" if perms else "unknown",
                    },
                )
        except Exception:
            try:
                self._objects.delete(key)
            except StorageError:
                logger.warning("orphan_upload_cleanup_failed")
            raise
        if self._dispatch is not None:
            self._dispatch(job_id, ctx.tenant_id)
        return document_id

    def new_version(
        self, ctx: AccessContext, document_id: UUID, *, filename: str, data: bytes
    ) -> int:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            self._require_admin(conn, ctx, document_id, manual=True)
            require_connector(conn, "upload")
        try:
            detected = detect(filename, data)
        except ParseFailed as exc:
            raise InvalidRequest(exc.code, exc.message) from exc
        digest = hashlib.sha256(data).hexdigest()
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            doc = store.load_document(conn, document_id)
            if doc is None or doc.deleted_at is not None:
                raise DocumentNotFound
            key = object_key(
                ctx.tenant_id, doc.source, document_id, doc.current_version + 1, filename
            )
            self._objects.put(key, data)
            version, job_id, _ = store.add_version(
                conn,
                tenant_id=ctx.tenant_id,
                document_id=document_id,
                filename=filename[:255],
                media_type=detected.media_type,
                byte_size=len(data),
                byte_sha256=digest,
                storage_key_for=lambda d, v: key,
                created_by=ctx.user_id,
            )
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="document.version",
                action="resource.admin",
                resource_id=document_id,
                decision="allow",
                reason="version_uploaded",
                details={"version": version},
            )
        if self._dispatch is not None:
            self._dispatch(job_id, ctx.tenant_id)
        return version

    def _require_admin(
        self, conn: Any, ctx: AccessContext, document_id: UUID, *, manual: bool = False
    ) -> store.DocumentRow:
        doc = store.load_document(conn, document_id)
        if doc is None or doc.deleted_at is not None:
            raise DocumentNotFound
        if not access.can_administer_document(conn, ctx.user_id, document_id).allowed:
            if access.can_read(conn, ctx.user_id, document_id).allowed:
                raise DocumentForbidden
            raise DocumentNotFound
        if manual and doc.source == "google_drive":
            # Sharing, versions and removal of a synced document come from Drive.
            raise InvalidRequest(
                "managed_by_connector",
                "This document is synced from Google Drive; change it in Drive and sync.",
            )
        return doc

    def set_permissions(self, ctx: AccessContext, document_id: UUID, spec: PermissionSpec) -> None:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            doc = self._require_admin(conn, ctx, document_id, manual=True)
            perms = self._resolve(conn, spec, doc.department)
            store.set_permissions(conn, ctx.tenant_id, document_id, perms)
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="document.permissions",
                action="resource.admin",
                resource_id=document_id,
                decision="allow",
                reason="permissions_set",
                details={"classification": spec.classification, "grants": len(perms.grants)},
            )

    def delete(self, ctx: AccessContext, document_id: UUID) -> None:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            self._require_admin(conn, ctx, document_id, manual=True)
            store.delete_document(conn, document_id)
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="document.delete",
                action="resource.admin",
                resource_id=document_id,
                decision="allow",
                reason="deleted",
            )
        try:  # eligibility already excludes the document; this only frees space
            self._qdrant.delete_points(
                collection_name(self._settings, self._model),
                {"must": [{"key": "document_id", "match": {"value": str(document_id)}}]},
            )
        except QdrantError:
            logger.warning("qdrant_delete_failed", extra={"document_id": str(document_id)})

    def reindex(self, ctx: AccessContext, document_id: UUID) -> None:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            self._require_admin(conn, ctx, document_id)
            job_id = store.reindex(conn, ctx.tenant_id, document_id)
        if self._dispatch is not None:
            self._dispatch(job_id, ctx.tenant_id)

    def visible(self, ctx: AccessContext) -> list[dict[str, Any]]:
        out = []
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            for doc in store.list_documents(conn):
                if doc.deleted_at is not None:
                    continue
                described = self._describe(conn, ctx, doc)
                if described is not None:
                    out.append(described)
        return out

    def get(self, ctx: AccessContext, document_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            doc = store.load_document(conn, document_id)
            described = self._describe(conn, ctx, doc) if doc and doc.deleted_at is None else None
        if described is None:
            raise DocumentNotFound
        return described

    def _describe(
        self, conn: Any, ctx: AccessContext, doc: store.DocumentRow
    ) -> dict[str, Any] | None:
        admin = access.can_administer_document(conn, ctx.user_id, doc.id).allowed
        reader = access.can_read(conn, ctx.user_id, doc.id).allowed
        if not admin and not reader:
            return None
        versions = store.list_versions(conn, doc.id)
        current = next((v for v in versions if v.version == doc.current_version), None)
        if not admin and (doc.active_generation is None):
            return None  # readers only see searchable documents
        return {
            "document_id": str(doc.id),
            "title": doc.title,
            "department": doc.department,
            "classification": doc.classification,
            "permission_state": doc.permission_state,
            "current_version": doc.current_version,
            "searchable": doc.active_generation is not None and doc.permission_state == "verified",
            "status": current.status if current else "unknown",
            "error": (
                {"code": current.error_code, "message": current.error_message}
                if current and current.error_code
                else None
            ),
            "can_administer": admin,
            "can_read": reader,
            "versions": [
                {
                    "version": v.version,
                    "filename": v.filename,
                    "status": v.status,
                    "error_code": v.error_code,
                    "byte_size": v.byte_size,
                    "created_at": v.created_at.isoformat(),
                    "parser": v.parser,
                    "parse_notes": v.parse_notes or {},
                }
                for v in versions
            ]
            if admin
            else [],
        }


# -- worker-side runner ----------------------------------------------------------------------


def _transient(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ModelError):
        return (
            "embedding_unavailable",
            "Waiting for the embedding service (model gateway); indexing will retry.",
        )
    if isinstance(exc, StorageError):
        return "storage_unavailable", "Waiting for file storage; indexing will retry."
    return "index_unavailable", "Waiting for the search index; indexing will retry."


class IngestionRunner:
    """Executes ingestion jobs. ``fault`` is a test hook called with a stage name
    (``after_chunks``, ``after_upsert``, ``before_activate``) to simulate crashes."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        objects: ObjectStore,
        qdrant: QdrantClient,
        model: ModelClient,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._objects = objects
        self._qdrant = qdrant
        self._model = model
        self.fault = fault

    def _audit(
        self, conn: Any, job: store.JobRow, decision: str, reason: str, **details: Any
    ) -> None:
        record_audit_event(
            conn,
            request_id=None,
            tenant_id=job.tenant_id,
            event_type="document.ingest",
            action="ingest",
            resource_id=job.document_id,
            decision=decision,
            reason=reason,
            details={"version": job.version, "generation": job.generation, **details},
        )

    def _fault(self, stage: str) -> None:
        if self.fault is not None:
            self.fault(stage)

    def run_job(self, job_id: UUID, tenant_id: UUID) -> str:
        space = self._model.embedding_space()
        collection = collection_name(self._settings, self._model)
        with tenant_transaction(self._engine, tenant_id) as conn:
            job = store.claim_job(conn, job_id, self._settings.ingestion_job_lease_seconds)
            if job is None:
                return "not_claimed"
            doc = store.load_document(conn, job.document_id)
            if doc is None or doc.deleted_at is not None:
                store.finish_job(conn, job.id)
                self._audit(conn, job, "info", "document_deleted")
                return "deleted"
            if job.version != doc.current_version:
                store.finish_job(conn, job.id)
                self._audit(conn, job, "info", "superseded")
                return "superseded"
            if job.attempts > self._settings.ingestion_job_max_attempts:
                store.set_version_status(
                    conn,
                    job.document_id,
                    job.version,
                    "failed",
                    "ingestion_failed",
                    "Indexing failed after several attempts.",
                )
                if job.generation:
                    store.fail_generation(conn, job.document_id, job.generation)
                store.finish_job(conn, job.id)
                self._audit(conn, job, "info", "attempts_exhausted")
                return "attempts_exhausted"
            generation = job.generation or store.next_generation(conn, job.document_id)
            store.set_job_generation(conn, job.id, generation)
            store.stage_generation(
                conn,
                tenant_id,
                job.document_id,
                generation,
                job.version,
                CHUNKER_VERSION,
                f"{space.model}@{space.revision}",
                collection,
            )
            store.set_version_status(conn, job.document_id, job.version, "parsing")
            version = store.load_version(conn, job.document_id, job.version)
        assert version is not None  # noqa: S101 - FK guarantees it
        job = store.JobRow(
            job.id,
            job.tenant_id,
            job.document_id,
            job.version,
            generation,
            job.kind,
            job.status,
            job.attempts,
        )
        try:
            return self._index(job, doc, version, collection, space.dense_size)
        except (StorageError, ModelError, QdrantError) as exc:
            # Transient: leave the staging generation inactive and retry later, with a
            # status that says what it is waiting for.
            code, message = _transient(exc)
            with tenant_transaction(self._engine, tenant_id) as conn:
                store.set_version_status(
                    conn, job.document_id, job.version, "queued", code, message
                )
                store.release_job(conn, job.id)
                self._audit(conn, job, "info", "retry_scheduled", error=type(exc).__name__)
            return "retry"

    def _index(  # noqa: PLR0915 - one linear, documented pipeline
        self,
        job: store.JobRow,
        doc: store.DocumentRow,
        version: store.VersionRow,
        collection: str,
        dense_size: int,
    ) -> str:
        assert job.generation is not None  # noqa: S101
        try:
            canonical = self._canonical(version)
        except ParseFailed as exc:
            status = "needs_ocr" if exc.code == "needs_ocr" else "failed"
            with tenant_transaction(self._engine, job.tenant_id) as conn:
                store.set_version_status(
                    conn, job.document_id, job.version, status, exc.code, exc.message
                )
                store.fail_generation(conn, job.document_id, job.generation)
                store.finish_job(conn, job.id)
                self._audit(conn, job, "info", f"parse_{exc.code}")
            return status
        if version.text_sha256 is not None and version.text_sha256 != canonical.text_sha256:
            with tenant_transaction(self._engine, job.tenant_id) as conn:
                store.set_version_status(
                    conn,
                    job.document_id,
                    job.version,
                    "failed",
                    "parser_changed",
                    "Re-parsing produced different text; upload a new version instead.",
                )
                store.fail_generation(conn, job.document_id, job.generation)
                store.finish_job(conn, job.id)
            return "failed"
        chunks = chunk_document(canonical)
        rows = self._chunk_rows(job, doc, canonical, chunks)
        with tenant_transaction(self._engine, job.tenant_id) as conn:
            store.set_canonical(
                conn,
                job.document_id,
                job.version,
                canonical.text,
                canonical.text_sha256,
                canonical.parser,
                canonical.notes,
                elements_to_json(canonical),
            )
            store.stage_generation(
                conn,
                job.tenant_id,
                job.document_id,
                job.generation,
                job.version,
                CHUNKER_VERSION,
                *self._space_strings(),
                collection,
            )
            store.insert_chunks(conn, job.tenant_id, rows)
            store.set_version_status(conn, job.document_id, job.version, "indexing")
        self._fault("after_chunks")

        vectors = self._model.embed_text([c.text for c in chunks])
        self._qdrant.ensure_collection(collection, dense_size)
        points = [
            {
                "id": str(row["point_id"]),
                "vector": {
                    "dense": vector.dense,
                    "sparse": SparseVector(
                        list(vector.sparse), list(vector.sparse.values())
                    ).as_json(),
                },
                "payload": {
                    "tenant_id": str(job.tenant_id),
                    "document_id": str(job.document_id),
                    "version": job.version,
                    "generation": job.generation,
                },
            }
            for row, vector in zip(rows, vectors, strict=True)
        ]
        for start in range(0, len(points), 64):
            self._qdrant.upsert(collection, points[start : start + 64])
            self._fault("after_upsert")
        generation_filter = {
            "must": [
                {"key": "tenant_id", "match": {"value": str(job.tenant_id)}},
                {"key": "document_id", "match": {"value": str(job.document_id)}},
                {"key": "generation", "match": {"value": job.generation}},
            ]
        }
        stored = self._qdrant.count(collection, generation_filter)
        self._fault("before_activate")
        with tenant_transaction(self._engine, job.tenant_id) as conn:
            in_db = store.count_chunks(conn, job.document_id, job.generation)
            if stored != len(rows) or in_db != len(rows):
                raise QdrantError(f"validation failed: qdrant={stored} db={in_db} want={len(rows)}")
            ok, reason = store.activate_generation(
                conn, job.document_id, job.generation, job.version
            )
            if ok:
                store.set_version_status(conn, job.document_id, job.version, "ready")
            else:
                store.fail_generation(conn, job.document_id, job.generation)
            store.finish_job(conn, job.id)
            self._audit(conn, job, "info", reason, chunks=len(rows), parser=canonical.parser)
            retired = store.retired_generations(conn, job.document_id)
        for generation, coll in retired:
            try:
                self._qdrant.delete_points(
                    coll,
                    {
                        "must": [
                            {"key": "document_id", "match": {"value": str(job.document_id)}},
                            {"key": "generation", "match": {"value": generation}},
                        ]
                    },
                )
            except QdrantError:
                logger.warning("retired_generation_cleanup_failed")
        if not ok:
            try:
                self._qdrant.delete_points(collection, generation_filter)
            except QdrantError:
                logger.warning("refused_generation_cleanup_failed")
        return "ready" if ok else reason

    def _docling(self, filename: str, data: bytes) -> dict[str, Any]:
        """Docling on the gateway (M1 parser choice for PDFs). Any model-side failure
        becomes ``DoclingUnavailable`` so ``parse`` uses the documented fallback."""

        if not self._settings.use_docling:
            raise DoclingUnavailable("disabled")
        try:
            return self._model.parse_document(filename, data)
        except GatewayNotConfigured as exc:
            raise DoclingUnavailable("not_configured") from exc
        except GatewayTimeout as exc:
            raise DoclingUnavailable("timeout") from exc
        except ModelError as exc:
            raise DoclingUnavailable(type(exc).__name__) from exc

    def _canonical(self, version: store.VersionRow) -> CanonicalDocument:
        """Stored elements win: reindexing and retries rebuild the exact canonical
        document that was cited, without re-running a parser that may have changed or
        be unavailable. Otherwise parse the verified bytes."""

        if version.canonical_text is not None and version.elements is not None:
            assert version.text_sha256 is not None and version.parser is not None  # noqa: S101
            return from_stored(
                version.canonical_text,
                version.text_sha256,
                version.elements,
                version.parser,
                version.parse_notes,
            )
        data = read_verified(self._objects, version.storage_key, version.byte_sha256)
        return parse(version.filename, data, docling=self._docling)

    def _space_strings(self) -> tuple[str]:
        space = self._model.embedding_space()
        return (f"{space.model}@{space.revision}",)

    @staticmethod
    def _chunk_rows(
        job: store.JobRow,
        doc: store.DocumentRow,
        canonical: CanonicalDocument,
        chunks: list[ChunkSpec],
    ) -> list[dict[str, Any]]:
        assert job.generation is not None  # noqa: S101
        rows = []
        for chunk in chunks:
            body = canonical.text[chunk.char_start : chunk.char_end]
            assert chunk.text == chunk.prefix + body  # noqa: S101 - chunker invariant
            rows.append(
                {
                    "point_id": point_id(
                        job.tenant_id,
                        doc.source,
                        job.document_id,
                        job.version,
                        job.generation,
                        chunk.ordinal,
                    ),
                    "document_id": job.document_id,
                    "version": job.version,
                    "generation": job.generation,
                    "ordinal": chunk.ordinal,
                    "text": chunk.text,
                    "prefix": chunk.prefix,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "page": chunk.page,
                    "heading_path": list(chunk.heading_path),
                    "element_type": chunk.element_type,
                    "content_sha256": chunk.content_sha256,
                }
            )
        return rows
