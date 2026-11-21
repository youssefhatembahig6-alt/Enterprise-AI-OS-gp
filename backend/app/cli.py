"""Developer/operator commands: migrate, seed-dev, export-openapi.

Usage: python -m app.cli <command> [options]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _owner_url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        sys.exit("MIGRATION_DATABASE_URL (privileged migration role) is required")
    return url


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "app/db/migrations"))
    config.attributes["url"] = _owner_url()
    command.upgrade(config, "head")
    print("migrations: at head")


def cmd_seed_dev(args: argparse.Namespace) -> None:
    from app.fixtures import load_foundation
    from app.seed import SeedRefused, seed_foundation, write_credentials

    environment = os.environ.get("EAIOS_ENV", "development")
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    try:
        result = seed_foundation(
            engine,
            load_foundation(),
            environment=environment,
            reset_passwords=args.reset_passwords,
        )
    except SeedRefused as exc:
        sys.exit(f"refused: {exc}")
    finally:
        engine.dispose()
    written = write_credentials(Path(args.credentials_file), result.new_credentials)
    print("seed: inserted rows", json.dumps(result.created, sort_keys=True))
    if written:
        print(
            f"seed: wrote {written} new development password(s) to {args.credentials_file}"
            " (owner-only permissions; passwords are not printed)"
        )
    else:
        print("seed: no new users; existing passwords unchanged")


def _business_source(args: argparse.Namespace) -> Any:
    from app.business_data.source import verify_vendored, verify_zip

    return verify_zip(Path(args.zip)) if args.zip else verify_vendored()


def cmd_business_report(args: argparse.Namespace) -> None:
    """Verify the source and print provenance, counts, fingerprint and anomalies."""

    from app.business_data.model import dataset_fingerprint, normalize, source_anomalies

    source = _business_source(args)
    dataset = normalize(source)
    manifest = source.manifest
    report = {
        "source_form": source.form,
        "provenance": {
            k: manifest[k]
            for k in ("tenant", "generator_version", "source_revision", "seed", "reference_date")
        },
        "source_manifest_sha256": source.manifest_sha256,
        "source_content_fingerprint": manifest["content_fingerprint"],
        "source_key_counts": {
            k: manifest["row_counts"][k]
            for k in ("companies", "users", "documents", "orders", "invoices")
        },
        "normalized_row_counts": dataset.row_counts(),
        "normalized_fingerprint": dataset_fingerprint(dataset),
        "source_anomalies": source_anomalies(
            dataset, date.fromisoformat(manifest["reference_date"])
        ).counts,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_import_business_data(args: argparse.Namespace) -> None:
    from app.business_data.loader import ImportRefused, import_sales_dataset
    from app.business_data.model import normalize

    source = _business_source(args)
    dataset = normalize(source)
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    try:
        result = import_sales_dataset(
            engine, source, dataset, environment=os.environ.get("EAIOS_ENV", "development")
        )
    except ImportRefused as exc:
        sys.exit(f"refused: {exc}")
    finally:
        engine.dispose()
    print("business-data: source verified (" + source.form + ")")
    print("business-data: inserted rows", json.dumps(result.inserted, sort_keys=True))
    print("business-data: tenant row counts", json.dumps(result.row_counts, sort_keys=True))
    print("business-data: fingerprint", result.database_fingerprint, "(matches source)")


def cmd_vendor_business_data(args: argparse.Namespace) -> None:
    from app.business_data.source import VENDORED_DIR, vendor_subset

    out = Path(args.output) if args.output else VENDORED_DIR
    manifest = vendor_subset(Path(args.zip), out)
    from app.business_data.model import dataset_fingerprint, normalize
    from app.business_data.source import verify_vendored

    manifest["normalized_fingerprint"] = dataset_fingerprint(normalize(verify_vendored(out)))
    (out / "vendor-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"vendored {len(manifest['vendored_tables'])} tables to {out}")


def cmd_retrieval_eval(args: argparse.Namespace) -> None:
    """Dense vs dense+sparse retrieval on the frozen M3 dev cases (real Qdrant)."""

    from app.config import Settings
    from app.documents.evaluate import markdown_summary, run_retrieval_eval
    from app.documents.qdrant import QdrantClient
    from app.reporting.model_client import build_model_client

    settings = Settings(
        DATABASE_URL=os.environ.get("DATABASE_URL", "postgresql+psycopg://unused@localhost/none"),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        QDRANT_URL=args.qdrant,
        S3_ENDPOINT_URL=os.environ.get("S3_ENDPOINT_URL", "http://localhost:8333"),
        JWT_SECRET=os.environ.get("JWT_SECRET", "retrieval-eval-only-not-a-real-secret-0"),
    )
    model = build_model_client(settings)
    qdrant = QdrantClient(args.qdrant, timeout=60.0)
    docling = None
    if args.docling:
        from app.documents.parsers import DoclingUnavailable
        from app.reporting.model_client import ModelError

        def docling(filename: str, data: bytes) -> dict[str, Any]:
            try:
                return model.parse_document(filename, data)
            except ModelError as exc:
                raise DoclingUnavailable(type(exc).__name__) from exc

    try:
        record = run_retrieval_eval(model, qdrant, top_k=args.top_k, docling=docling)
    finally:
        qdrant.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(markdown_summary(record))
    print(f"retrieval-eval: wrote {out} (inference_mode={record['inference_mode']})")


def _m1_shape(text: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """The application's canonical document in the M1 scorer's PDF output shape."""

    tables, headings = [], []
    for element in elements:
        body = text[element["start"] : element["end"]]
        if element["type"] == "table":
            tables.append(
                [[cell.strip() for cell in line.split(" | ")] for line in body.split("\n")]
            )
        elif element["type"] == "heading":
            headings.append(body)
    return {"text": text, "tables": tables, "headings": headings}


def cmd_parser_export(args: argparse.Namespace) -> None:
    """Export the stored canonical text of every PDF version (owner role) so the M1
    scorer can check reading order, tables and OCR as ingested by the application."""

    from sqlalchemy import text

    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT d.id, d.title, v.version, v.filename, v.status, v.error_code, v.parser,
                       v.parse_notes, v.byte_sha256, v.text_sha256, v.canonical_text, v.elements
                  FROM documents d JOIN document_versions v ON v.document_id = d.id
                 WHERE lower(v.filename) LIKE '%.pdf' AND d.deleted_at IS NULL
                 ORDER BY d.title, v.version
                """
            )
        ).all()
    engine.dispose()
    record = []
    for r in rows:
        item = {
            "document_id": str(r.id),
            "title": r.title,
            "version": r.version,
            "filename": r.filename,
            "status": r.status,
            "error_code": r.error_code,
            "parser": r.parser,
            "parse_notes": r.parse_notes or {},
            "byte_sha256": r.byte_sha256,
            "text_sha256": r.text_sha256,
            "elements": len(r.elements or []),
            "element_types": sorted({e["type"] for e in (r.elements or [])}),
        }
        if r.canonical_text is not None:
            item["out"] = _m1_shape(r.canonical_text, r.elements or [])
        record.append(item)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    for item in record:
        print(
            f"{item['status']:<10} {item['parser'] or '-':<22}"
            f" {item['filename']}  ({item['title']})"
        )
    print(f"parser-export: wrote {out}")


def cmd_relevance_study(args: argparse.Namespace) -> None:
    """Lexical gate versus BGE-M3 dense similarity on the labelled study pairs."""

    from app.config import Settings
    from app.documents.relevance import run_relevance_study, study_markdown
    from app.reporting.model_client import build_model_client

    settings = Settings(
        DATABASE_URL=os.environ.get("DATABASE_URL", "postgresql+psycopg://unused@localhost/none"),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        QDRANT_URL=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        S3_ENDPOINT_URL=os.environ.get("S3_ENDPOINT_URL", "http://localhost:8333"),
        JWT_SECRET=os.environ.get("JWT_SECRET", "relevance-study-only-not-a-real-secret-0"),
    )
    record = run_relevance_study(build_model_client(settings))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(study_markdown(record))
    print(f"relevance-study: wrote {out} (inference_mode={record['inference_mode']})")


def cmd_add_drive_reader(args: argparse.Namespace) -> None:
    """Development only (M4 live Drive demo): add a tenant user whose email is a real
    Google account, copying attributes, roles and groups from an existing user, so a
    Drive share to that account maps to a tenant user. The password goes to the
    owner-only credentials file, never to stdout."""

    import secrets

    from sqlalchemy import text

    from app.security.passwords import hash_password
    from app.seed import write_credentials

    if os.environ.get("EAIOS_ENV", "development") not in ("development", "test"):
        sys.exit("refused: development only")
    email = args.email.strip().lower()
    password = secrets.token_urlsafe(18)
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    try:
        with engine.begin() as conn:
            like = conn.execute(
                text(
                    "SELECT u.* FROM users u JOIN tenants t ON t.id = u.tenant_id"
                    " WHERE t.slug = :slug AND u.email = :like"
                ),
                {"slug": args.tenant, "like": args.like.lower()},
            ).one_or_none()
            if like is None:
                sys.exit("refused: --like user not found in that tenant")
            new_id = conn.execute(
                text(
                    "INSERT INTO users (tenant_id, email, display_name, password_hash, status,"
                    " clearance, country, department) VALUES (:t, :e, :n, :h, 'active', :c,"
                    " :co, :d) ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id"
                ),
                {
                    "t": like.tenant_id,
                    "e": email,
                    "n": args.display_name,
                    "h": hash_password(password),
                    "c": like.clearance,
                    "co": like.country,
                    "d": like.department,
                },
            ).scalar()
            if new_id is None:
                print("add-drive-reader: user already exists; nothing changed")
                return
            conn.execute(
                text(
                    "INSERT INTO user_roles (tenant_id, user_id, role_id, scope_department)"
                    " SELECT tenant_id, :new, role_id, scope_department FROM user_roles"
                    " WHERE user_id = :like"
                ),
                {"new": new_id, "like": like.id},
            )
            conn.execute(
                text(
                    "INSERT INTO group_memberships (tenant_id, group_id, user_id)"
                    " SELECT tenant_id, group_id, :new FROM group_memberships WHERE user_id = :like"
                ),
                {"new": new_id, "like": like.id},
            )
    finally:
        engine.dispose()
    write_credentials(Path(args.credentials_file), {args.tenant: {email: password}})
    print(f"add-drive-reader: added {email}; password written to {args.credentials_file}")


def cmd_export_openapi(args: argparse.Namespace) -> None:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://openapi@localhost/none",
        REDIS_URL="redis://localhost:6379/0",
        QDRANT_URL="http://localhost:6333",
        S3_ENDPOINT_URL="http://localhost:8333",
        JWT_SECRET="openapi-export-only-not-a-real-secret-000",  # noqa: S106
    )
    schema = create_app(settings).openapi()
    Path(args.output).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"openapi: wrote {args.output}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head (owner role)").set_defaults(
        func=cmd_migrate
    )
    seed = sub.add_parser("seed-dev", help="idempotent development fixture seed (owner role)")
    seed.add_argument("--credentials-file", default=str(BACKEND_ROOT / "var/dev-credentials.json"))
    seed.add_argument(
        "--reset-passwords",
        action="store_true",
        help="regenerate passwords for fixture users (development only)",
    )
    seed.set_defaults(func=cmd_seed_dev)
    for name, func, help_text in (
        ("business-data-report", cmd_business_report, "verify the NileTech source; print report"),
        ("import-business-data", cmd_import_business_data, "import NileTech sales (owner role)"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--zip", help="original export ZIP (default: vendored subset)")
        command.set_defaults(func=func)
    vendor = sub.add_parser("vendor-business-data", help="write the verified sales subset")
    vendor.add_argument("--zip", required=True)
    vendor.add_argument("--output", help="default: fixtures/business/niletech-0.1.6")
    vendor.set_defaults(func=cmd_vendor_business_data)
    evaluate = sub.add_parser("retrieval-eval", help="measure dense vs hybrid retrieval")
    evaluate.add_argument("--qdrant", default=os.environ.get("QDRANT_URL", "http://localhost:6333"))
    evaluate.add_argument("--top-k", type=int, default=5)
    evaluate.add_argument(
        "--docling",
        action="store_true",
        help="parse PDFs with the gateway's Docling route, as ingestion does",
    )
    evaluate.add_argument("--out", default=str(BACKEND_ROOT.parent / "var" / "retrieval-eval.json"))
    evaluate.set_defaults(func=cmd_retrieval_eval)

    study = sub.add_parser("relevance-study", help="lexical gate vs dense similarity")
    study.add_argument("--out", default=str(BACKEND_ROOT.parent / "var" / "relevance-study.json"))
    study.set_defaults(func=cmd_relevance_study)

    parsed = sub.add_parser("parser-export", help="stored PDF canonical text for M1 scoring")
    parsed.add_argument("--out", default=str(BACKEND_ROOT.parent / "var" / "parser-export.json"))
    parsed.set_defaults(func=cmd_parser_export)

    reader = sub.add_parser("add-drive-reader", help="dev: tenant user for the live Drive demo")
    reader.add_argument("--tenant", default="niletech")
    reader.add_argument("--email", required=True, help="the Google account shared in Drive")
    reader.add_argument("--like", default="finance.reader@niletech.example")
    reader.add_argument("--display-name", default="Drive demo reader")
    reader.add_argument(
        "--credentials-file", default=str(BACKEND_ROOT / "var/dev-credentials.json")
    )
    reader.set_defaults(func=cmd_add_drive_reader)
    export = sub.add_parser("export-openapi", help="write the OpenAPI schema")
    export.add_argument("--output", default=str(BACKEND_ROOT.parent / "docs/openapi.json"))
    export.set_defaults(func=cmd_export_openapi)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
