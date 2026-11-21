# Admin console, Drive sources and tenant configuration contract (M4)

Prepared 21 November 2026. Decisions: [ADR 0401](../adr/0401-drive-sync-and-freshness.md) (Drive sync and freshness), [ADR 0402](../adr/0402-scoped-administration.md) (scoped administration), [ADR 0403](../adr/0403-tenant-configuration.md) (packs and connectors). Schema: [openapi.json](../openapi.json).

Errors use the usual `{"error": {code, message, request_id}}`: missing admin capability → `403 forbidden`; an object outside the caller's scope → `404 not_found`; a pack or connector the tenant has not enabled → `403 capability_disabled`; invalid input → `422` with a specific code.

## Routes

| Route | Needs | Purpose |
| --- | --- | --- |
| `GET /api/v1/tenant/settings` | signed in | Enabled packs/connectors, `sync_mode`, `sync_interval_seconds`, `max_staleness_seconds`, `version` |
| `PUT /api/v1/admin/tenant/settings` `{enabled_packs?, enabled_connectors?, sync_mode?}` | `tenant.config` tenant-wide | New settings version; raises the policy epoch |
| `GET /api/v1/admin/users` | `identity.admin` | Users in scope with roles, groups and attributes |
| `PATCH /api/v1/admin/users/{id}` `{status?, clearance?, country?, department?, display_name?}` | `identity.admin` covering the user (and the new department) | Not for yourself when deactivating |
| `PUT /api/v1/admin/users/{id}/roles` `{assignments: [{role, scope_department?}]}` | `identity.admin`; no escalation (ADR 0402) | Replace role assignments; not your own |
| `PUT /api/v1/admin/users/{id}/groups` `{groups: [key]}` | `identity.admin` tenant-wide | Replace group memberships |
| `GET /api/v1/admin/roles` · `PUT /api/v1/admin/roles/{key}` `{name, capabilities}` | `identity.admin` (define: tenant-wide, capabilities you hold) | Role definitions |
| `GET /api/v1/admin/groups` · `PUT /api/v1/admin/groups/{key}/links` `{google_groups}` | `identity.admin` (links: tenant-wide) | Link Google group emails to an internal group |
| `GET /api/v1/admin/documents` | `resource.admin` | Documents in scope: source, sharing (`allow:group:…`, `allow:user:…`), `permissions_current`, status. Metadata only |
| `GET /api/v1/admin/sources` · `GET /api/v1/admin/sources/{id}` | `resource.admin` covering the department | Sources with `access`, `last_verified_sync_at`, schedule; detail adds files (state, unmapped principals) and the last 10 runs |
| `POST /api/v1/admin/sources` `{name, external_root, department, classification, department_restricted?, allowed_countries?}` | `resource.admin` + `google_drive` connector + the folder in the operator's `allowed_roots` | Create a Drive folder source (`201`) |
| `PATCH /api/v1/admin/sources/{id}` | as above | Rename, change attributes (denied until resynced), `status`: `active`/`paused`/`deleted` |
| `POST /api/v1/admin/sources/{id}/sync` | as above | Sync now. Always returns the `run_id` of the run this request created. Inline dispatch also returns its outcome; Celery returns `status: "queued"` |
| `GET /api/v1/admin/sources/{id}/runs/{run_id}` | as above | That run: `status` (`queued`, `running`, `succeeded`, `failed`, `skipped`, `superseded`), `terminal`, times, `error_code`, `stats` |
| `GET /api/v1/admin/audit?limit&before&event_type&resource_id` | `audit.read` | Newest first, scoped; `next_before` pages. URL-encode `before` (it contains `+`) |

`access` on a source: `current` (content served), `stale` (last verified sync older than the bound), `last_sync_failed` (including `root_not_allowed`), `resync_required` (an admin changed the source's policy and any running sync was fenced out, or the database was upgraded to 0009), `never_verified`, `paused`, `connector_disabled`. Content is served only while `access = current` and the item's own permissions were readable.

Existing M3 routes are unchanged, except: `PUT …/permissions`, `POST …/versions` and `DELETE` on a Drive-synced document return `422 managed_by_connector`; upload and new versions need the `upload` connector; `POST /api/v1/chat` returns `403 capability_disabled` for an explicit disabled pack. Runs that hit a disabled pack at start or at publication end with an `error` event `capability_disabled` and no content.

## Drive connector configuration (operator)

`DRIVE_CONNECTOR_CONFIG` is the path to a JSON file readable by the API and the worker (Compose mounts `./var/connectors` at `/run/eaios/connectors`, read-only; `var/` is git-ignored):

```json
{"tenants": {"niletech": {
  "auth": {"type": "service_account", "key_file": "/run/eaios/connectors/drive-sa.json"},
  "allowed_roots": ["<Drive folder id>"],
  "group_membership": "unavailable"}}}
```

or `"auth": {"type": "oauth_refresh_token", "client_id": "…", "client_secret": "…", "refresh_token": "…"}`. The scope is `https://www.googleapis.com/auth/drive.readonly`. The file is re-read on every use; removing a root makes that source's next sync fail before any Drive request, and its content is then denied. `group_membership` defaults to `unavailable`, which leaves every Drive group share unresolved. `directory` (Workspace only) adds the `admin.directory.group.member.readonly` scope and verifies direct `USER` members; any doubt still leaves the share unresolved. A service account must be added to the folder as an **Editor**: Drive returns a file's full permission list only to principals who can share it. A reader-only credential makes every item `permissions_unknown`, which denies it.

## Sync semantics

See ADR 0401. Per run:
1. Check the root against the current allowlist.
2. List the tree.
3. Read every file's permissions and verify Google group membership.
4. Download only changed content.
5. Apply creates, versions, sharing, renames and moves.
6. Tombstone what disappeared.
7. Record `last_verified_sync_at` (the run start) and `permissions_current`.

Every write runs in a transaction fenced on the run's claim token and the source's policy version. A Drive group share becomes access only while the Google group is linked to the internal group and the user is in that internal group: unlinking or removing the user applies immediately, with no sync. A redelivered manual run id is recognised. It is a duplicate while the run is live or finished, and it is recovered with `attempt + 1` once the lease expires. Supported content: the M3 formats, plus Google Docs (exported as text) and Google Sheets (exported as CSV). Other Google-native files are listed as `unsupported` and never searchable.
