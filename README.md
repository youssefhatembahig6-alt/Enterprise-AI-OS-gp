# Enterprise AI OS

Application repository for the Enterprise AI OS graduation project. **M0** delivered the runnable foundation (tenant isolation, login, reference authorization policy) and **M1** the NileTech data baseline and feasibility measurements. **M2** adds the first feature: login → router → read-only SQL through a separate restricted role → report → verification → verified result streamed over SSE, with an authorized evidence view ([contract](docs/contracts/reports.md)). **M3** adds authorized document answers: upload with permissions, deterministic ingestion into Qdrant, retrieval filtered by current permissions on every search branch, and verified answers whose citations open the exact passage of the version cited ([contract](docs/contracts/documents.md)). **M4** adds Google Drive folder sync whose content is served only while the last verified sync is current, a scoped admin console (documents, sources, users, roles, audit) and per-tenant packs and connectors enforced in the backend ([contract](docs/contracts/admin.md)). GitHub, the legal pack and the desktop shell come in M5.

| Path | Contents |
| --- | --- |
| `backend/` | FastAPI app (`app/api`, `services`, `repositories`, `security`, `tasks`, `db`), Alembic migrations, fixtures, tests |
| `apps/web/` | React + Vite + Tailwind sign-in and identity screen |
| `packages/ui/` | Shared React components and theme tokens (reused by the later Electron shell) |
| `scripts/` | Portable Python helpers: `dev.py`, `smoke_stack.py`, `m2_demo_capture.py` |
| `ml/model-gateway/` | Colab GPU gateway (chat/embed/check/health) used by M2 |
| `infra/postgres/` | First-run role bootstrap for the PostgreSQL container |
| `docs/` | ADRs, contracts, runbook, OpenAPI schema and traceability |

Quick start: [docs/runbook.md](docs/runbook.md). Decisions: [docs/adr/](docs/adr/README.md). API schema: [docs/openapi.json](docs/openapi.json).

## Pull request reviews

Implement each milestone on its own branch, run the assignment checks and open a pull request targeting `main`. Include test results and unresolved checklist items. Share the PR link in the planning chat for review before merging.
