# ADR 0403 — Tenant configuration: packs and connectors

Status: Accepted for M4 (pending planning review). Relates to: source ADR 0010 (core, packs and tenant configuration), FR-12.

## Decision

- `tenant_settings` holds `enabled_packs` ⊆ {`documents`, `sales_reports`}, `enabled_connectors` ⊆ {`upload`, `google_drive`} and `sync_mode` ∈ {`demo`, `nightly`}. The sync interval and staleness bound are generated from `sync_mode` in the database. The vocabularies are code (`app/tenancy/config.py`). A tenant without a row has nothing enabled. Migration 0007 gave existing tenants their pre-M4 behaviour.
- Settings come from the fixture (`[tenants.settings]` in `foundation.toml`) or the tenant-wide `tenant.config` API. No tenant name or slug appears in application code.
- **Backend enforcement at every entry point**, always against the current row:
  - `POST /api/v1/chat`: an explicit `mode` for a disabled pack → `403 capability_disabled`, and no run is created. `auto` routes only to enabled packs.
  - Run creation rechecks the pack (defence in depth).
  - The worker refuses the run at start if its pack is disabled, and again at publication under the policy-epoch lock. Settings changes raise the epoch, so a pack switched off mid-run publishes nothing (`capability_disabled`).
  - Upload and new versions need `upload`; sources and syncs need `google_drive`. Drive content is denied while `google_drive` is disabled.
- The web UI hides menus for disabled packs and connectors. That is presentation only.

## Demonstrated configurations (fixtures)

| Tenant | Packs | Connectors | Sync |
| --- | --- | --- | --- |
| `niletech` | documents, sales_reports | upload, google_drive | demo (15 min, deny after 30 min) |
| `isolation-test` | documents | upload | nightly (02:00 UTC, deny after 30 h) |
