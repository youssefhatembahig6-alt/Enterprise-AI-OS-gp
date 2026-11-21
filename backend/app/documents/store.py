"""Document, version, generation, chunk, job and answer-source persistence.

All functions run inside ``tenant_transaction`` (row-level security). Callers decide
authorization before calling; this module only enforces data invariants.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


@dataclass(frozen=True, slots=True)
class Permissions:
    classification: str
    department: str
    department_restricted: bool
    allowed_countries: tuple[str, ...]
    # (effect, user_id or None, group_id or None)
    grants: tuple[tuple[str, UUID | None, UUID | None], ...]


@dataclass(frozen=True, slots=True)
class VersionRow:
    tenant_id: UUID
    document_id: UUID
    version: int
    filename: str
    media_type: str
    byte_size: int
    byte_sha256: str
    storage_key: str
    status: str
    error_code: str | None
    error_message: str | None
    canonical_text: str | None
    text_sha256: str | None
    parser: str | None
    parse_notes: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    elements: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: UUID
    tenant_id: UUID
    title: str
    source: str
    current_version: int
    active_generation: int | None
    deleted_at: datetime | None
    created_by: UUID | None
    created_at: datetime
    department: str
    classification: str
    permission_state: str
    resource_status: str


@dataclass(frozen=True, slots=True)
class ChunkRow:
    point_id: UUID
    document_id: UUID
    version: int
    generation: int
    ordinal: int
    text: str
    prefix: str
    char_start: int
    char_end: int
    page: int | None
    heading_path: list[str]
    element_type: str


_DOC_SELECT = """
    SELECT d.id, d.tenant_id, d.title, d.source, d.current_version, d.active_generation,
           d.deleted_at, d.created_by, d.created_at, r.department, r.classification,
           r.permission_state, r.status AS resource_status
      FROM documents d JOIN resources r ON r.tenant_id = d.tenant_id AND r.id = d.id
"""


def _doc(row: Any) -> DocumentRow:
    return DocumentRow(
        id=row.id,
        tenant_id=row.tenant_id,
        title=row.title,
        source=row.source,
        current_version=row.current_version,
        active_generation=row.active_generation,
        deleted_at=row.deleted_at,
        created_by=row.created_by,
        created_at=row.created_at,
        department=row.department,
        classification=row.classification,
        permission_state=row.permission_state,
        resource_status=row.resource_status,
    )


def _insert_job(
    conn: Connection, tenant_id: UUID, document_id: UUID, version: int, kind: str
) -> UUID:
    job_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO ingestion_jobs (id, tenant_id, document_id, version, kind, status)"
            " VALUES (:id, :t, :d, :v, :k, 'pending')"
        ),
        {"id": job_id, "t": tenant_id, "d": document_id, "v": version, "k": kind},
    )
    return job_id


def _write_grants(conn: Connection, tenant_id: UUID, resource_id: UUID, perms: Permissions) -> None:
    conn.execute(
        text("DELETE FROM resource_grants WHERE resource_id = :r AND action = 'resource.read'"),
        {"r": resource_id},
    )
    for effect, user_id, group_id in perms.grants:
        conn.execute(
            text(
                "INSERT INTO resource_grants"
                " (tenant_id, resource_id, user_id, group_id, effect, action)"
                " VALUES (:t, :r, :u, :g, :e, 'resource.read')"
            ),
            {"t": tenant_id, "r": resource_id, "u": user_id, "g": group_id, "e": effect},
        )


def create_document(
    conn: Connection,
    *,
    tenant_id: UUID,
    title: str,
    filename: str,
    media_type: str,
    byte_size: int,
    byte_sha256: str,
    storage_key_for: Any,
    permissions: Permissions | None,
    unknown_department: str,
    created_by: UUID | None,
    source: str = "upload",
    document_id: UUID | None = None,
) -> tuple[UUID, UUID, str]:
    """Create the protected resource, its grants, the document, version 1 and its
    ingestion job, all in the caller's transaction. Permissions are therefore saved
    before any job can be dispatched. ``permissions=None`` stores the resource with
    ``permission_state = 'unknown'`` (most restrictive values), which no content
    action can pass until permissions are set."""

    document_id = document_id or uuid.uuid4()
    p = permissions
    conn.execute(
        text(
            """
            INSERT INTO resources (id, tenant_id, kind, external_ref, title, classification,
                department, department_restricted, allowed_countries, permission_state)
            VALUES (:id, :t, 'document', :ref, :title, :cls, :dept, :restricted, :countries, :state)
            """
        ),
        {
            "id": document_id,
            "t": tenant_id,
            "ref": f"{source}:{document_id}",
            "title": title,
            "cls": p.classification if p else "restricted",
            "dept": p.department if p else unknown_department,
            "restricted": p.department_restricted if p else True,
            "countries": list(p.allowed_countries) if p else ["ZZ"],
            "state": "verified" if p else "unknown",
        },
    )
    if p:
        _write_grants(conn, tenant_id, document_id, p)
    conn.execute(
        text(
            "INSERT INTO documents (tenant_id, id, source, title, current_version, created_by)"
            " VALUES (:t, :id, :source, :title, 1, :u)"
        ),
        {"t": tenant_id, "id": document_id, "source": source, "title": title, "u": created_by},
    )
    key = storage_key_for(document_id, 1)
    _insert_version(
        conn,
        tenant_id,
        document_id,
        1,
        filename,
        media_type,
        byte_size,
        byte_sha256,
        key,
        created_by,
    )
    job_id = _insert_job(conn, tenant_id, document_id, 1, "ingest")
    return document_id, job_id, key


def _insert_version(
    conn: Connection,
    tenant_id: UUID,
    document_id: UUID,
    version: int,
    filename: str,
    media_type: str,
    byte_size: int,
    byte_sha256: str,
    key: str,
    created_by: UUID | None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO document_versions (tenant_id, document_id, version, filename, media_type,
                byte_size, byte_sha256, storage_key, status, created_by)
            VALUES (:t, :d, :v, :f, :m, :n, :sha, :key, 'queued', :u)
            """
        ),
        {
            "t": tenant_id,
            "d": document_id,
            "v": version,
            "f": filename,
            "m": media_type,
            "n": byte_size,
            "sha": byte_sha256,
            "key": key,
            "u": created_by,
        },
    )


def add_version(
    conn: Connection,
    *,
    tenant_id: UUID,
    document_id: UUID,
    filename: str,
    media_type: str,
    byte_size: int,
    byte_sha256: str,
    storage_key_for: Any,
    created_by: UUID | None,
) -> tuple[int, UUID, str]:
    row = conn.execute(
        text(
            "SELECT current_version FROM documents WHERE id = :d AND deleted_at IS NULL FOR UPDATE"
        ),
        {"d": document_id},
    ).one()
    version = int(row.current_version) + 1
    key = storage_key_for(document_id, version)
    _insert_version(
        conn,
        tenant_id,
        document_id,
        version,
        filename,
        media_type,
        byte_size,
        byte_sha256,
        key,
        created_by,
    )
    conn.execute(
        text(
            "UPDATE documents SET current_version = :v, updated_at = clock_timestamp()"
            " WHERE id = :d"
        ),
        {"v": version, "d": document_id},
    )
    return version, _insert_job(conn, tenant_id, document_id, version, "ingest"), key


def reindex(conn: Connection, tenant_id: UUID, document_id: UUID) -> UUID:
    row = conn.execute(
        text("SELECT current_version FROM documents WHERE id = :d AND deleted_at IS NULL"),
        {"d": document_id},
    ).one()
    return _insert_job(conn, tenant_id, document_id, int(row.current_version), "reindex")


def set_permissions(
    conn: Connection, tenant_id: UUID, document_id: UUID, perms: Permissions
) -> None:
    conn.execute(
        text(
            """
            UPDATE resources SET classification = :cls, department = :dept,
                   department_restricted = :restricted, allowed_countries = :countries,
                   permission_state = 'verified', updated_at = now()
             WHERE id = :id
            """
        ),
        {
            "cls": perms.classification,
            "dept": perms.department,
            "restricted": perms.department_restricted,
            "countries": list(perms.allowed_countries),
            "id": document_id,
        },
    )
    _write_grants(conn, tenant_id, document_id, perms)


def delete_document(conn: Connection, document_id: UUID) -> None:
    """Tombstone: the resource is marked deleted (bumping the policy epoch) and the
    document keeps ``deleted_at`` so a late ingestion retry cannot reactivate it."""

    conn.execute(
        text("UPDATE resources SET status = 'deleted', updated_at = now() WHERE id = :d"),
        {"d": document_id},
    )
    conn.execute(
        text(
            "UPDATE documents SET deleted_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE id = :d"
        ),
        {"d": document_id},
    )
    conn.execute(
        text(
            "UPDATE document_generations SET status = 'retired'"
            " WHERE document_id = :d AND status IN ('active', 'staging')"
        ),
        {"d": document_id},
    )


def load_document(conn: Connection, document_id: UUID) -> DocumentRow | None:
    row = conn.execute(text(_DOC_SELECT + " WHERE d.id = :d"), {"d": document_id}).one_or_none()
    return _doc(row) if row else None


def lock_documents(conn: Connection, document_ids: list[UUID]) -> dict[UUID, DocumentRow]:
    rows = conn.execute(
        text(_DOC_SELECT + " WHERE d.id = ANY(:ids) ORDER BY d.id FOR SHARE OF d"),
        {"ids": document_ids},
    ).all()
    return {r.id: _doc(r) for r in rows}


def list_documents(conn: Connection) -> list[DocumentRow]:
    rows = conn.execute(text(_DOC_SELECT + " ORDER BY d.created_at, d.id")).all()
    return [_doc(r) for r in rows]


def searchable_documents(conn: Connection) -> list[DocumentRow]:
    rows = conn.execute(
        text(
            _DOC_SELECT + " WHERE d.deleted_at IS NULL AND d.active_generation IS NOT NULL"
            " AND r.status = 'active'"
        )
    ).all()
    return [_doc(r) for r in rows]


def load_version(conn: Connection, document_id: UUID, version: int) -> VersionRow | None:
    row = conn.execute(
        text("SELECT * FROM document_versions WHERE document_id = :d AND version = :v"),
        {"d": document_id, "v": version},
    ).one_or_none()
    if row is None:
        return None
    return VersionRow(
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        version=row.version,
        filename=row.filename,
        media_type=row.media_type,
        byte_size=row.byte_size,
        byte_sha256=row.byte_sha256,
        storage_key=row.storage_key,
        status=row.status,
        error_code=row.error_code,
        error_message=row.error_message,
        canonical_text=row.canonical_text,
        text_sha256=row.text_sha256,
        parser=row.parser,
        parse_notes=row.parse_notes or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
        elements=row.elements,
    )


def list_versions(conn: Connection, document_id: UUID) -> list[VersionRow]:
    rows = conn.execute(
        text("SELECT version FROM document_versions WHERE document_id = :d ORDER BY version"),
        {"d": document_id},
    ).all()
    return [v for r in rows if (v := load_version(conn, document_id, r.version)) is not None]


def set_version_status(
    conn: Connection,
    document_id: UUID,
    version: int,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        text(
            "UPDATE document_versions SET status = :s, error_code = :c, error_message = :m,"
            " updated_at = clock_timestamp() WHERE document_id = :d AND version = :v"
        ),
        {"s": status, "c": error_code, "m": error_message, "d": document_id, "v": version},
    )


def set_canonical(
    conn: Connection,
    document_id: UUID,
    version: int,
    canonical: str,
    sha: str,
    parser: str,
    notes: dict[str, Any],
    elements: list[dict[str, Any]] | None = None,
) -> None:
    """Store a version's canonical text once; element spans are stored once too (also
    for versions parsed before elements were kept, provided the text hash matches)."""

    conn.execute(
        text(
            """
            UPDATE document_versions SET canonical_text = :txt, text_sha256 = :sha, parser = :p,
                   parse_notes = CAST(:notes AS jsonb), updated_at = clock_timestamp()
             WHERE document_id = :d AND version = :v AND canonical_text IS NULL
            """
        ),
        {
            "txt": canonical,
            "sha": sha,
            "p": parser,
            "notes": json.dumps(notes, sort_keys=True),
            "d": document_id,
            "v": version,
        },
    )
    if elements is not None:
        conn.execute(
            text(
                """
                UPDATE document_versions SET elements = CAST(:e AS jsonb)
                 WHERE document_id = :d AND version = :v AND elements IS NULL
                   AND text_sha256 = :sha
                """
            ),
            {"e": json.dumps(elements), "d": document_id, "v": version, "sha": sha},
        )


# -- generations and chunks ------------------------------------------------------------


def next_generation(conn: Connection, document_id: UUID) -> int:
    value = conn.execute(
        text(
            "SELECT coalesce(max(generation), 0) + 1 FROM document_generations"
            " WHERE document_id = :d"
        ),
        {"d": document_id},
    ).scalar_one()
    return int(value)


def stage_generation(
    conn: Connection,
    tenant_id: UUID,
    document_id: UUID,
    generation: int,
    version: int,
    chunker: str,
    embed_model: str,
    collection: str,
) -> None:
    """Create (or reset, on retry) a staging generation. Chunk rows of the same
    generation are removed so a retry rewrites them exactly once."""

    status = conn.execute(
        text("SELECT status FROM document_generations WHERE document_id = :d AND generation = :g"),
        {"d": document_id, "g": generation},
    ).scalar_one_or_none()
    if status is not None and status != "staging":
        raise RuntimeError(f"generation {generation} is {status}, not staging")
    conn.execute(
        text("DELETE FROM document_chunks WHERE document_id = :d AND generation = :g"),
        {"d": document_id, "g": generation},
    )
    if status is None:
        conn.execute(
            text(
                """
                INSERT INTO document_generations (tenant_id, document_id, generation, version,
                    status, chunker, embed_model, collection)
                VALUES (:t, :d, :g, :v, 'staging', :c, :e, :col)
                """
            ),
            {
                "t": tenant_id,
                "d": document_id,
                "g": generation,
                "v": version,
                "c": chunker,
                "e": embed_model,
                "col": collection,
            },
        )


def insert_chunks(conn: Connection, tenant_id: UUID, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            text(
                """
                INSERT INTO document_chunks (tenant_id, point_id, document_id, version, generation,
                    ordinal, text, prefix, char_start, char_end, page, heading_path, element_type,
                    content_sha256)
                VALUES (:t, :point_id, :document_id, :version, :generation, :ordinal, :text,
                    :prefix, :char_start, :char_end, :page, :heading_path, :element_type,
                    :content_sha256)
                """
            ),
            {"t": tenant_id, **row},
        )
    if rows:
        conn.execute(
            text(
                "UPDATE document_generations SET chunk_count = :n"
                " WHERE document_id = :d AND generation = :g"
            ),
            {"n": len(rows), "d": rows[0]["document_id"], "g": rows[0]["generation"]},
        )


def count_chunks(conn: Connection, document_id: UUID, generation: int) -> int:
    return int(
        conn.execute(
            text("SELECT count(*) FROM document_chunks WHERE document_id = :d AND generation = :g"),
            {"d": document_id, "g": generation},
        ).scalar_one()
    )


def activate_generation(
    conn: Connection, document_id: UUID, generation: int, version: int
) -> tuple[bool, str]:
    """Atomically switch the document's active generation. Refuses (returning a
    reason) if the document was deleted or a newer version superseded this one."""

    doc = conn.execute(
        text("SELECT current_version, deleted_at FROM documents WHERE id = :d FOR UPDATE"),
        {"d": document_id},
    ).one()
    if doc.deleted_at is not None:
        return False, "deleted"
    if int(doc.current_version) != version:
        return False, "superseded"
    conn.execute(
        text(
            "UPDATE document_generations SET status = 'retired'"
            " WHERE document_id = :d AND status = 'active'"
        ),
        {"d": document_id},
    )
    conn.execute(
        text(
            "UPDATE document_generations SET status = 'active', activated_at = clock_timestamp()"
            " WHERE document_id = :d AND generation = :g AND status = 'staging'"
        ),
        {"d": document_id, "g": generation},
    )
    conn.execute(
        text(
            "UPDATE documents SET active_generation = :g, updated_at = clock_timestamp()"
            " WHERE id = :d"
        ),
        {"g": generation, "d": document_id},
    )
    return True, "activated"


def fail_generation(conn: Connection, document_id: UUID, generation: int) -> None:
    conn.execute(
        text(
            "UPDATE document_generations SET status = 'failed'"
            " WHERE document_id = :d AND generation = :g AND status = 'staging'"
        ),
        {"d": document_id, "g": generation},
    )


def retired_generations(conn: Connection, document_id: UUID) -> list[tuple[int, str]]:
    rows = conn.execute(
        text(
            "SELECT generation, collection FROM document_generations"
            " WHERE document_id = :d AND status IN ('retired', 'failed')"
        ),
        {"d": document_id},
    ).all()
    return [(r.generation, r.collection) for r in rows]


def active_point_ids(conn: Connection, document_ids: list[UUID]) -> list[UUID]:
    if not document_ids:
        return []
    rows = conn.execute(
        text(
            """
            SELECT c.point_id FROM document_chunks c
              JOIN documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id
                              AND c.generation = d.active_generation
             WHERE c.document_id = ANY(:ids) AND d.deleted_at IS NULL
             ORDER BY c.point_id
            """
        ),
        {"ids": document_ids},
    ).all()
    return [r.point_id for r in rows]


def load_chunks(conn: Connection, point_ids: list[UUID]) -> dict[UUID, ChunkRow]:
    if not point_ids:
        return {}
    rows = conn.execute(
        text(
            """
            SELECT point_id, document_id, version, generation, ordinal, text, prefix, char_start,
                   char_end, page, heading_path, element_type
              FROM document_chunks WHERE point_id = ANY(:ids)
            """
        ),
        {"ids": point_ids},
    ).all()
    return {
        r.point_id: ChunkRow(
            point_id=r.point_id,
            document_id=r.document_id,
            version=r.version,
            generation=r.generation,
            ordinal=r.ordinal,
            text=r.text,
            prefix=r.prefix,
            char_start=r.char_start,
            char_end=r.char_end,
            page=r.page,
            heading_path=list(r.heading_path or []),
            element_type=r.element_type,
        )
        for r in rows
    }


# -- jobs --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobRow:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    version: int
    generation: int | None
    kind: str
    status: str
    attempts: int


def claim_job(conn: Connection, job_id: UUID, lease_seconds: int) -> JobRow | None:
    row = conn.execute(
        text(
            """
            UPDATE ingestion_jobs
               SET status = 'claimed', attempts = attempts + 1,
                   claimed_until = clock_timestamp() + make_interval(secs => :lease),
                   updated_at = clock_timestamp()
             WHERE id = :id
               AND (status = 'pending'
                    OR (status = 'claimed' AND claimed_until < clock_timestamp()))
            RETURNING id, tenant_id, document_id, version, generation, kind, status, attempts
            """
        ),
        {"id": job_id, "lease": lease_seconds},
    ).one_or_none()
    if row is None:
        return None
    return JobRow(
        row.id,
        row.tenant_id,
        row.document_id,
        row.version,
        row.generation,
        row.kind,
        row.status,
        row.attempts,
    )


def set_job_generation(conn: Connection, job_id: UUID, generation: int) -> None:
    conn.execute(
        text("UPDATE ingestion_jobs SET generation = :g WHERE id = :id AND generation IS NULL"),
        {"g": generation, "id": job_id},
    )


def finish_job(conn: Connection, job_id: UUID) -> None:
    conn.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'done', claimed_until = NULL,"
            " updated_at = clock_timestamp() WHERE id = :id"
        ),
        {"id": job_id},
    )


def release_job(conn: Connection, job_id: UUID) -> None:
    """Return a claimed job to pending for a later retry (transient failure). The
    dispatch time is set to now, so the beat re-dispatches it after one interval
    instead of immediately."""

    conn.execute(
        text(
            "UPDATE ingestion_jobs SET status = 'pending', claimed_until = NULL,"
            " last_dispatched_at = clock_timestamp(), updated_at = clock_timestamp()"
            " WHERE id = :id"
        ),
        {"id": job_id},
    )


def mark_job_dispatched(conn: Connection, job_id: UUID) -> None:
    conn.execute(
        text(
            "UPDATE ingestion_jobs SET last_dispatched_at = clock_timestamp(),"
            " updated_at = clock_timestamp() WHERE id = :id AND status = 'pending'"
        ),
        {"id": job_id},
    )


# -- answer sources -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceRow:
    run_id: UUID
    citation_id: str
    document_id: UUID
    version: int
    generation: int
    point_id: UUID
    char_start: int
    char_end: int
    text_sha256: str
    quote_sha256: str


def insert_source(conn: Connection, tenant_id: UUID, source: SourceRow) -> None:
    conn.execute(
        text(
            """
            INSERT INTO answer_sources (tenant_id, run_id, citation_id, document_id, version,
                generation, point_id, char_start, char_end, text_sha256, quote_sha256)
            VALUES (:t, :run, :cid, :d, :v, :g, :p, :s, :e, :tsha, :qsha)
            """
        ),
        {
            "t": tenant_id,
            "run": source.run_id,
            "cid": source.citation_id,
            "d": source.document_id,
            "v": source.version,
            "g": source.generation,
            "p": source.point_id,
            "s": source.char_start,
            "e": source.char_end,
            "tsha": source.text_sha256,
            "qsha": source.quote_sha256,
        },
    )


def load_source(conn: Connection, run_id: UUID, citation_id: str) -> SourceRow | None:
    row = conn.execute(
        text("SELECT * FROM answer_sources WHERE run_id = :r AND citation_id = :c"),
        {"r": run_id, "c": citation_id},
    ).one_or_none()
    if row is None:
        return None
    return SourceRow(
        row.run_id,
        row.citation_id,
        row.document_id,
        row.version,
        row.generation,
        row.point_id,
        row.char_start,
        row.char_end,
        row.text_sha256,
        row.quote_sha256,
    )
