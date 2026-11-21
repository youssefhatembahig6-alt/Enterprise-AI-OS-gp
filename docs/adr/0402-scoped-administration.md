# ADR 0402 — Scoped administration

Status: Accepted for M4 (pending planning review). Relates to: source ADR 0014 (admin console, Proposed), FR-11, roadmap "Users/roles changes and deactivation …".

## Decision

Admin rights are role capabilities with the existing `user_roles.scope_department` (`NULL` = tenant-wide). None of them implies content read.

| Capability | Covers | Scoped holder | Tenant-wide holder |
| --- | --- | --- | --- |
| `resource.admin` | documents and connector sources | own departments only | all |
| `identity.admin` | users, role assignments | users of own departments; assignments scoped inside own scope | all users; group membership, group links, role definitions |
| `audit.read` | audit activity | events on resources/sources of own departments, or by users of those departments | every tenant event |
| `tenant.config` | packs, connectors, sync schedule | — (tenant-wide only) | yes |

- **No escalation.** Assigning or removing an assignment that carries an administrative capability requires the assigner to hold it for that scope. Content capabilities (`resource.read`, `dataset.query`) may be assigned within scope because they grant nothing without resource grants. Role definitions may only contain capabilities the definer holds tenant-wide.
- **No lock-out.** An administrator cannot deactivate themselves or change their own role assignments. A role **definition** edit is checked on the caller's *effective* result before it commits: all of their assignments, with the edited role carrying its new capabilities. If no tenant-wide assignment would still grant `identity.admin`, the edit is rejected (`cannot_remove_own_admin`). A department-scoped `identity.admin` does not count, because definitions are a tenant-wide operation.
- **Scope does not leak.** Users, documents and sources outside the caller's scope return `404`. Missing capability returns `403`.
- **Removal takes effect at the next authorization point.** The access context is rebuilt from PostgreSQL on every request, and every user, role, group and grant change raises the tenant policy epoch. Running answers recheck at publication under the epoch lock (M2/M3), and reopening a citation or replaying a run re-evaluates current access. Deactivation fails the next request with `401`.
- **Audit.** Every admin change writes an audit event in the same transaction. The application role can now `SELECT` audit rows, but only inside its own tenant (RLS); scope filtering happens in the API. Audit rows never carry document text.

## Fixture roles

- `department_admin` (NileTech and the isolation tenant): `resource.admin`, `identity.admin` and `audit.read`, assigned with scope `finance`.
- `access_admin` (NileTech): `resource.admin`, `identity.admin`, `audit.read` and `tenant.config`, tenant-wide.
