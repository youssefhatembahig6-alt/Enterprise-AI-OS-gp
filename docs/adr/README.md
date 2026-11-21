# Architecture decision records (repository)

ADR numbers 0001–0020 belong to the decision table in section 13 of the
[project specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f). That table marks some rows **Proposed**; this
repository does not change their status, and nothing here records a team
meeting. Repository ADRs start at 0101 so they never collide with the source list.

| ADR | Title | Status | Relates to source |
| --- | --- | --- | --- |
| [0101](0101-identity-and-session.md) | Identity and session contract | Accepted for M0 (pending planning review) | §8 JWT authentication, FR-01, NFR-05 |
| [0102](0102-tenant-boundary.md) | Tenant boundary in PostgreSQL | Accepted for M0 (pending planning review) | §8 multi-tenant isolation, ADR 0003, NFR-01 |
| [0103](0103-authorization-reference-policy.md) | Reference authorization policy | Accepted for M0 (pending planning review) | §8 five layers, ADR 0004, ADR 0012 (Proposed) |
| [0104](0104-retrieval-eligible-ids-design.md) | Eligible-ID retrieval design (design only) | Proposed; implemented as ADR 0303 | ADR 0004, ADR 0012 (Proposed), FR-02 |
| [0105](0105-runtime-and-compose.md) | Runtime, pinned versions and Compose | Accepted for M0 (pending planning review) | §7, ADR 0007, 0008, 0013, NFR-03/04 |
| [0201](0201-restricted-report-sql.md) | Restricted report SQL execution | Accepted for M2 (pending planning review) | §8 safe text-to-SQL, FR-07, T6 |
| [0202](0202-report-authorization-and-row-scope.md) | Report authorization and row scope | Accepted for M2 (pending planning review) | §8 five layers, ADR 0012 (Proposed) |
| [0203](0203-report-runs-events-publication.md) | Runs, events, publication boundary and jobs | Accepted for M2 (pending planning review) | §7 streaming/outbox, ADR 0006, 0011, 0018 |
| [0204](0204-report-verification.md) | Report verification and repair bound | Accepted for M2 (pending planning review) | §6 hybrid verification, ADR 0005, 0018, FR-09 |
| [0301](0301-document-ingestion-and-generations.md) | Document ingestion, generations and safe retries | Accepted for M3 (pending planning review) | §9 deterministic ingestion, safe publishing, FR-05 |
| [0302](0302-parser-fallback-and-chunking.md) | Parsing and chunking for M3 | Accepted for M3 (pending planning review) | ADR 0016 (Proposed), M1 parser report |
| [0303](0303-eligible-id-retrieval.md) | Authorization inside retrieval (eligible point ids) | Accepted for M3 (pending planning review) | ADR 0004, 0012 (Proposed), completes 0104, T9 |
| [0304](0304-citations-and-refusals.md) | Exact citations and indistinguishable refusals | Accepted for M3 (pending planning review) | §8 refusals, §9 citations, FR-03, FR-04, T10 |
| [0401](0401-drive-sync-and-freshness.md) | Google Drive sync and permission freshness | Accepted for M4 (pending planning review) | FR-10, FR-02, roadmap M4 connector details |
| [0402](0402-scoped-administration.md) | Scoped administration | Accepted for M4 (pending planning review) | ADR 0014 (Proposed), FR-11 |
| [0403](0403-tenant-configuration.md) | Tenant configuration: packs and connectors | Accepted for M4 (pending planning review) | ADR 0010, FR-12 |
