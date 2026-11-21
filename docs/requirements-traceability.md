# Requirement → test traceability (M0–M3)

M0 implements FR-01 and the foundations of FR-02 and FR-11, plus NFR-01, NFR-03 and NFR-05. Document, search and SQL behaviour (most of T1–T10) belongs to later milestones and is **not** marked complete here.

| Requirement | What M0 delivers | Evidence |
| --- | --- | --- |
| FR-01 Identity | Login, Argon2id, JWT validation, per-request server-built context, logout, deactivation, rate limit | `tests/integration/test_auth_api.py`, `tests/unit/test_tokens_and_redaction.py`, `scripts/smoke_stack.py` |
| FR-02 Filtered retrieval (foundation) | Reference five-layer evaluator, declarative fixtures, DB-backed decisions; retrieval design only | `tests/unit/test_policy_evaluator.py`, `tests/unit/test_policy_fixtures.py`, `tests/integration/test_authorization_db.py`, ADR 0103/0104 |
| FR-11 Audit (foundation) | `audit_events` for login allow/deny/rate-limit, token rejection, logout and authorization decisions; app role insert-only | `test_auth_api.py::test_audit_events_and_no_secret_leakage`, `test_authorization_db.py::test_authorization_decisions_are_audited`, `test_tenant_isolation.py::test_audit_events_readable_only_inside_own_tenant` (M4 opened tenant-scoped reads for the admin console) |
| NFR-01 Isolation (foundation) | RLS on every tenant table for a non-owner role, composite cross-tenant FKs, transaction-scoped context, pool cleanup | `tests/integration/test_tenant_isolation.py` |
| NFR-03 Compose | One `docker compose up --build`, health-gated, named volumes, restart persistence | `compose.yaml`, runbook, PR acceptance log |
| NFR-05 Secrets | Secrets only in `.env`/env, `SecretStr`, log redaction, no tokens in browser storage, no secrets in logs or responses | `test_auth_api.py::test_audit_events_and_no_secret_leakage`, `test_health.py`, `test_tokens_and_redaction.py`, ESLint storage rule |

## M1 data fixtures

| Requirement | What M1 delivers | Evidence |
| --- | --- | --- |
| NFR-06 Dataset (foundation) | Verified NileTech 0.1.6 sales baseline (manifest, hashes, provenance pinned), deterministic normalization and fingerprint | `tests/unit/test_business_source.py`, `tests/integration/test_business_import.py::test_two_clean_database_generations_are_identical` |
| FR-07 / FR-14 (inputs for M2) | Explicit repeatable import; reconciled lines, orders and invoices; frozen Q1 2026 evaluation case with a hand-checked subset | `tests/unit/test_sales_reference.py`, `tests/integration/test_business_import.py`, `docs/data/sales-report-q1-2026.md` |
| NFR-01 Isolation | RLS and read-only grants on sales tables; isolation tenant sees no business rows | `test_business_import.py::test_isolation_tenant_and_missing_context_see_no_business_rows` |

## M1 parser comparison

| Requirement | What M1 delivers | Evidence |
| --- | --- | --- |
| FR-05 Deterministic ingestion (input) / ADR 0016 | Parser benchmark on digital, two-column, table-heavy and scanned PDFs plus CSV and workbook cases; seven synthetic fixtures with SHA-256 provenance and hand-checked gold; laptop results and Colab results for the model-based candidates (explicit run selection in `results/selection.json`); recommendation: Docling for complex PDFs, Python `csv`, openpyxl | `ml/parser-benchmark/reports/2026-11-20-parser-comparison.md`, `ml/parser-benchmark/tests/`, CI job `parser-benchmark` |

## M2 verified SQL reports

Tests below are in `backend/tests/`. Integration tests run on real PostgreSQL 17.6 with deterministic model doubles (`FakeModelClient`, labelled fake); they are integration evidence, not model-quality evidence. The real-model demonstration is listed separately.

| Requirement | What M2 delivers | Evidence |
| --- | --- | --- |
| FR-07 Read-only SQL | Separate `eaios_report` role (non-owner, no superuser/BYPASSRLS), `reporting` security-invoker views, restrictive RLS bound to an application-minted backend context, parsed allowlist, read-only transaction, timeouts, row limit | `integration/test_report_sql_security.py`, `unit/test_report_sql_guard.py`, ADR 0201 |
| FR-07 / FR-14 Report semantics | Rule-derived intent from tenant profile; AST checks of date column/window, statuses, measures, grain, filters; join-multiplicity probe; exact decimal result checks; frozen Q1 2026 plus three independently calculated periods | `unit/test_report_semantics.py`, `unit/test_sales_generalization_reference.py`, `integration/test_report_workflow.py::test_frozen_q1_report_is_published_with_evidence`, `::test_other_periods_use_runtime_verification_not_stored_answers`, ADR 0204 |
| FR-06 Agent routing | LangGraph graph prepare → route → SQL → check → execute → evidence → draft → verify → publish/finish; rules first, model router only for unrecognised topics | `app/reporting/workflow.py`, `test_report_workflow.py::test_unsupported_or_ambiguous_requests_abstain`, `::test_unavailable_inference_fails_without_partial_output[router]` |
| FR-09 Verification | Deterministic claim checks plus development NLI baseline; ≤ 2 repairs; failed drafts never stored or streamed; structured-only publication when narrative cannot be verified | `test_report_workflow.py::test_bad_sql_is_repaired_and_never_shown`, `::test_exhausted_sql_repairs_abstain_without_figures`, `::test_bad_narrative_is_repaired_and_hidden`, `::test_exhausted_narrative_repairs_publish_structured_only`, `::test_checker_rejection_hides_unsupported_narrative`, `::test_checker_failure_publishes_only_deterministic_results` |
| FR-03 Progress and citations (SQL part) | Six SSE events, persisted with increasing ids; only `status` before publication; table cells and citations reference immutable evidence by row/column; evidence view with hash integrity | `report_support.assert_only_status_before_publication`, `test_report_workflow.py::test_reconnect_replays_identical_events`, `apps/web/src/ReportPanel.tsx` |
| FR-02/NFR-01 Authorization and isolation | `dataset.query` through the five-layer policy; denied same-tenant users and the isolation tenant get one indistinguishable abstention; revocation at four run stages and before publication; revoked replay and evidence; foreign ids 404 | `fixtures/policy_cases.toml`, `test_report_workflow.py::test_denied_and_isolated_users_get_the_same_abstention`, `::test_revocation_during_a_run_prevents_publication`, `::test_replay_and_evidence_after_revocation_expose_nothing`, `::test_foreign_and_unknown_ids_are_not_found`, ADR 0202/0203 |
| FR-11 Audit | `report.run/authorize/query/publish/finish/replay/evidence` events with decisions and private reasons, no figures or SQL text | `test_report_workflow.py::test_audit_records_report_decisions_without_figures` |
| NFR-02 Responsiveness | Progress streams immediately; answer only after verification; stage timings recorded per run (`diagnostics.timings`) | `scripts/m2_demo_capture.py` |
| NFR-04 Model endpoint | One root, explicit chat/embed/check/health paths, bearer service auth, timeouts, cancellation, typed failures, pinned revisions; unavailable gateway fails clearly | `unit/test_model_client.py`, `unit/test_model_gateway_contract.py`, `test_report_workflow.py::test_unconfigured_gateway_reports_model_unavailable` |
| Jobs and durability | Run + job row in one transaction; Celery dispatch; beat re-dispatch; claim/lease; unique terminal event | `test_report_workflow.py::test_idempotent_create_and_duplicate_job_delivery`, `::test_outbox_redispatches_a_lost_job` |
| Review fixes (PR #5) | Value-changing casts rejected (AST and runtime scale); per-event SSE reauthorization; query context bound to session identity + transaction secret (migration 0004) | `unit/test_report_semantics.py::test_value_changing_casts_are_rejected`, `test_report_workflow.py::test_rounded_amounts_never_receive_a_verified_label`, `::test_runtime_scale_check_blocks_rounding_the_ast_missed`, `::test_revocation_between_frames_of_one_batch_stops_content`, `test_report_sql_security.py::test_stale_context_is_not_inherited_by_a_reused_pid`, `::test_crashed_backend_context_is_cleaned_up`, `::test_pooled_connection_with_lingering_row_sees_nothing` |
| **Real-model demonstration** | One end-to-end run on commit `1f1c127`: API, PostgreSQL 17.12 and the gateway together on a Colab T4 over loopback, inline dispatch; pinned Qwen3-8B-AWQ at 0.75 / 4096; published `figures_verified_narrative_dev_checked` with exact Q1 2026 figures and verified evidence | [`docs/evidence/m2/`](evidence/m2/README.md) (capture SHA-256 `192eb975…d490`) |

## M3 authorized document answers

Integration tests run on real PostgreSQL 17.6 and real Qdrant 1.15.4 with labelled fake embeddings and answerer; they are security and pipeline evidence, not retrieval-quality evidence.

| Requirement | What M3 delivers | Evidence |
| --- | --- | --- |
| FR-02 Filtered retrieval | Eligible point ids from the reference policy over current PostgreSQL state; tenant + `has_id` filter on dense, sparse, each prefetch and fusion; recheck before text and publication | `test_documents_answers.py::test_t9_*`, `::test_t1_*`, `::test_t2_t3_*`, ADR 0303 |
| FR-03 Citations | Exact code-point spans of the cited version, hashes rechecked on open, server-side slicing; UI source panel | `test_documents_answers.py::test_citations_use_code_point_offsets_for_tables_and_non_ascii`, `::test_repeated_passage_*`, `test_documents_ingestion.py::test_new_version_*`, ADR 0304 |
| FR-04 Indistinguishable refusal | One `no_sources` template; restricted vs removed over 30 runs each | `test_documents_answers.py::test_t10_restricted_and_removed_documents_are_indistinguishable` |
| FR-05 Deterministic ingestion | Permissions before scheduling, immutable versions, deterministic chunks and point ids, staged generations, atomic activation, tombstones | `test_documents_ingestion.py`, `unit/test_documents_parsing.py`, ADR 0301/0302 |
| FR-09 Verification (documents) | Exact quotes, numbers in quotes, development NLI checker, at most 2 repairs | `test_documents_answers.py::test_fabricated_quote_*`, `::test_one_repair_then_published`, `::test_checker_rejection_*` |
| FR-10 Connectors (upload) | Upload with permissions, versions, reindex, delete; SeaweedFS storage | `test_documents_ingestion.py`, `scripts/m3_demo.py` |
| FR-14 Evaluation | Frozen retrieval dev cases; `retrieval-eval` harness (dense vs hybrid) | `fixtures/evaluation/documents-dev-m3.toml`, `test_retrieval_eval.py` |
| **Retrieval quality (BGE-M3)** | 22 frozen cases with real BGE-M3 and Qdrant. Dense / hybrid recall@5 0.909 / 0.909 and MRR@5 0.886 / 0.841. Kept dense and the lexical gate. | [Retrieval and relevance results](evidence/m3/README.md#retrieval-and-relevance-decisions) |
| **Docling for complex PDFs** | Docling 2.151.1 through the gateway recovered 100% two-column reading order and table cells on the table-heavy fixture. The scan was ready with 97.2% text similarity. With the parse route unavailable, text-layer PDFs used pdfplumber and the scan was `needs_ocr`. | [Docling and fallback scores](evidence/m3/README.md#acceptance-checks), ADR 0302 |
| **Real-model document answer** | Qwen3-8B-AWQ in Colab gateway mode published an answer with an exact, verified versioned citation for finance. Sales received the standard `no_sources` response. | [Colab capture](evidence/m3/2026-11-21-colab-demo.json) |

## Security tests T1–T10 status

| Test | Status (updated for M3) |
| --- | --- |
| T1 / T2 | **M3:** finance reader retrieves the finance commentary with an exact citation; sales reader, explicit-deny reader and isolation tenant get `no_sources` and no markers (`test_documents_answers.py::test_t1_*`, `::test_t2_t3_*`). |
| T3 | **M3:** cross-tenant retrieval returns nothing; the isolation tenant reads only its own document (`::test_t3_isolation_tenant_reads_only_its_own_documents`); RLS on all document tables. |
| T4 | **M3:** internal group removal applies to the next request and during a run (before draft, verify, publish); revoked citations and replay expose nothing (`::test_t4_*`). External sync: M4. |
| T5 | **M3:** guessed document and citation ids return 404 for other users and tenants (`::test_t5_guessed_document_and_citation_ids_are_not_found`). File download endpoint: not provided. |
| T6 | **M2:** generated SQL cannot bypass database restrictions: validator rejections plus raw query-role attempts that bypass the validator (forged context, public objects, writes, DDL, borrowed context), cross-tenant predicates and joins, timeouts, row limits, pool reuse (`test_report_sql_security.py`). |
| T7 | **M3:** documents with unknown permissions are ingested but never retrieved until permissions are set (`::test_t7_*`). |
| T8 | **M3:** no answer cache exists; repeated questions after revocation are re-authorized and re-searched (`::test_t8_*`). |
| T9 | **M3:** asserted on the captured outbound Qdrant request for dense and hybrid modes; no request when nothing is eligible (`::test_t9_*`). |
| T10 | **M3:** restricted vs removed document, 30 interleaved runs each: identical normalized responses; median difference 3.4% in the latest run (`::test_t10_*`). |

## M4 — Drive sync, scoped administration, tenant configuration

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| FR-10 Connectors (Google Drive) | Folder sources, full ACL re-read per sync, fingerprint-incremental content, moves, removals, sharing changes; group shares only with verified Google membership; stale runs fenced; allowlist rechecked per sync; fail closed on listing/ACL failure and staleness ([ADR 0401](adr/0401-drive-sync-and-freshness.md)) | `test_m4_drive_sync.py` (Drive **double**), `test_drive_client.py` (HTTP client vs mocked API). **Live Drive run: open** — see `docs/evidence/m4/README.md` |
| FR-02 Filtered retrieval (connector freshness) | `permissions_current` computed in `load_resource_meta(s)` from source state, connector switch and staleness | `test_m4_drive_sync.py::test_failed_sync_…`, `::test_stale_verification_…`, `::test_paused_source_and_disabled_connector_…` |
| FR-11 Audit (scoped reads) | `audit.read` scope; admin changes audited in-transaction; no content in audit | `test_m4_admin.py::test_audit_activity_is_scoped`, `::test_admin_changes_are_audited` |
| FR-12 Configuration | `tenant_settings`; two fixture tenants with different packs, connectors and schedules; enforcement at API, run start and publication ([ADR 0403](adr/0403-tenant-configuration.md)) | `test_m4_tenant_config.py` |
| Admin scope (ADR 0014) | Department vs tenant-wide `resource.admin`/`identity.admin`/`audit.read`; no escalation; 404 outside scope; no self-lockout through assignments or role definitions ([ADR 0402](adr/0402-scoped-administration.md)) | `test_m4_admin.py` |
| Revocation semantics | Admin removals (deactivate, group, role, clearance) apply on the next request, before publication of an active run, and on citation reopen/replay; Drive unsharing applies after the next sync | `test_m4_admin.py::test_removal_*`, `test_m4_drive_sync.py::test_revoked_sharing_applies_after_the_next_sync` |
| NFR-07 UI | Admin console (Sources with last verified sync, Documents, Users, Roles & groups, Audit, Organization); menus follow tenant packs | `apps/web/src/AdminPanel.tsx`; lint/typecheck/build. Not exercised in a browser in this PR |
