# M4 acceptance evidence — Drive sync, scoped administration, tenant configuration

Prepared 21 November 2026 on branch `feature/m4-drive-admin-config`, starting from `origin/main` at `e875759`. Updated the same day after review of `943a3c9`: Drive group access now needs verified Google membership, stale syncs are fenced in the database, the operator allowlist is rechecked on every sync, role-definition edits cannot remove the editor's own admin rights, and the live runner waits for the exact run it requested.

Second review round (after `47200796`):
- an internal group removal or unlink now applies immediately to Drive documents;
- the upgrade to 0009 fails closed for Drive access granted under the earlier rules;
- redelivery of a manual sync run id is safe.

Third review round (after `16811379`):
- two Google groups linked to one internal group now grant independently (migration 0010);
- an abandoned run is closed as superseded when a different run takes over its expired lease.

**Labels.** "Drive double" means the in-memory test double in `tests/integration/drive_support.py`. It is not Google Drive. "Fake inference" means `MODEL_MODE=fake`. No result in this folder comes from live Google Drive.

## Status

| Acceptance item | Status | Evidence |
| --- | --- | --- |
| Drive sync: first import, incremental updates, moves, deletions, sharing changes | **Implemented; tested against the Drive double** | `test_m4_drive_sync.py` (37 tests), `test_drive_client.py` (15 tests, HTTP client against a mocked Drive API) |
| Drive group shares only with verified Google membership | Implemented and tested (double) | `test_group_access_needs_verified_current_google_membership`. The user stays in the internal group throughout. Unknown membership: denied. Verified member: allowed. Google membership revoked: denied. Unknown again after another successful sync: still denied. A direct share on the same file keeps working |
| Internal group removal or unlink applies immediately (no Drive sync) | Implemented and tested (Drive double); mutation-checked (without the live-membership join, 5 of the 7 tests fail) | `test_internal_group_removal_applies_without_a_drive_sync` (membership, link): fresh question `no_sources`, citation reopen 404, replay `access_revoked`, no new sync run. `test_internal_group_removal_blocks_an_active_answer` (membership at draft and at publish, link at publish). `test_direct_share_survives_internal_group_removal` (membership, link) |
| Upgrade fails closed | Implemented and tested | `test_m4_upgrade.py`: a database at 0007 (unverified group grant) and one at 0008 (group-derived user grant) are upgraded to head. The Drive document is denied (`deny.unknown_input`) before any sync, the source is `upgrade_resync_required`, the fingerprints are cleared, and the M3 upload-group document stays readable |
| Two Google groups linked to one internal group | Implemented and tested (Drive double); mutation-checked (without 0010 the sync fails `sync_internal_error`) | `test_two_google_groups_for_one_internal_group_are_independent` (unlink either address first): the sync succeeds with one row per address; unlinking one address keeps access; unlinking the last denies at once, with no sync run in between. `test_upgrade_from_0009_keys_group_grants_by_google_email` covers a database already at 0009 |
| Abandoned run when a different run takes over | Implemented and tested (Drive double); mutation-checked | `test_abandoned_run_is_superseded_when_another_run_takes_over`: manual run A dies, its lease expires, and scheduled run B succeeds. A is `superseded` (`claim_taken_over`) and has `finished_at` without ever being redelivered. A late delivery of A does no Drive work, B's run row and the source's verified time are unchanged, and the document has 1 version. Same-run recovery is still covered by `test_redelivery_recovers_a_dead_worker_without_applying_work_twice` |
| Manual sync redelivery | Implemented and tested (Drive double) | `test_duplicate_delivery_during_and_after_a_run`: no Drive work, one version, run `succeeded` attempt 1. `test_redelivery_recovers_a_dead_worker_without_applying_work_twice`: a duplicate before the lease expires; after expiry, recovery as attempt 2 ends `succeeded`, with each file at exactly 1 version. `test_older_attempt_cannot_overwrite_the_recovered_run`: the old attempt ends `superseded`, and the run row stays `succeeded` attempt 2 |
| Stale syncs cannot restore access | Implemented and tested (double); mutation-checked (with the fence disabled, all three tests fail) | `test_policy_change_during_a_sync_fences_the_running_sync`, `test_newer_worker_after_lease_expiry_fences_the_older_one`, `test_older_worker_cannot_tombstone_or_release_the_newer_claim` |
| Operator allowlist enforced on every sync | Implemented and tested (double) | `test_root_removed_from_allowlist_fails_before_any_drive_request`: no Drive call is made, the run fails `root_not_allowed`, and the previously readable document is denied |
| 15-minute demo / nightly schedule; last verified sync time shown | Implemented and tested | `test_schedule_is_fifteen_minutes_in_demo_and_nightly_otherwise`; Sources tab; Compose worker runs `dispatch_due_syncs` every minute |
| Deny when current permissions cannot be established | Implemented and tested | failed sync, subfolder failure, unreadable item ACL, stale verification, paused source, disabled connector |
| Scoped admin controls (documents, sources, users, roles, audit); department vs tenant scope in the backend | Implemented and tested | `test_m4_admin.py` (31 tests), including `test_role_edit_that_removes_own_identity_admin_is_rejected` (also with only a department-scoped `identity.admin` left) and `test_role_edit_is_allowed_when_another_assignment_preserves_rights` |
| Removing access: next request / before an active answer is published / on reopen | Implemented and tested for deactivation, group removal, role removal and clearance downgrade | `test_removal_applies_to_the_next_request`, `test_removal_during_an_active_run_blocks_publication` (draft, verify, publish), `test_deactivation_during_an_active_run_blocks_publication`, `test_removal_hides_published_answers_on_reopen_and_replay` |
| Two tenants with different packs and connectors, no code change; direct request for a disabled capability | Implemented and tested | `test_m4_tenant_config.py` (8 tests); [Compose smoke](2026-11-21-compose-smoke.json) |
| Live runner reliability (exact run correlation, document-specific citation, nonzero exit) | Implemented and tested without credentials | `test_m4_acceptance_runner.py` (5 tests: delayed worker, earlier success and scheduled run ignored, timeout exits 2 with evidence, wrong document exits 1); `test_sync_request_returns_its_own_run` |
| **Live Drive folder, direct user share: authorized answer → revoke sharing → sync → denied** | **OPEN — not performed (no app credentials, no second reader account)** | Runner ready: `scripts/m4_drive_acceptance.py`. Rehearsal with the Drive double: [revocation run](2026-11-21-drive-double-revocation.json) |
| Admin console browser walkthrough (read-only) | **Done for tenant admin and finance department admin**, with one recorded UI mismatch; Sources refusal **verified by API only** | See "Browser walkthrough" below |
| **Real Workspace group verification** (`group_membership: "directory"` against a real directory) | **OPEN, separate from the direct-share demo** | Needs a Google Workspace or Cloud Identity organization and the Directory read-only member scope. Only the mocked API is tested (`test_drive_client.py`) |

A fake Drive does not complete FR-10 (roadmap). The live item stays open until the runner passes against real Drive.

## Drive access check (step 1)

Checked on 21 November 2026 through the Google Drive connector available to this session. That connector is signed in as a **personal Gmail account**, not as the application.

- A dedicated private test folder now exists: **`eaios-m4-drive-test`**, folder id `1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L`. It contains `niletech-travel-policy.md` (file id `119GR0k59wCVdBRhQqzhWOpGIuAu9qC_e`), which answers the runner's default question. Nothing in it is shared.
- **File permissions: readable.** The permission list of the folder and of the file came back (owner only).
- **Group memberships: not available.** Consumer Google accounts have no API for Google Groups membership. The Admin SDK Directory and Cloud Identity Groups APIs need a Workspace or Cloud Identity organization. Without verified membership, the application leaves every Drive **group** share unresolved: it grants nobody, even when the group is linked to an internal group (ADR 0401). The live demo therefore uses a **user** share. With a Workspace directory, set `"group_membership": "directory"` in the operator file.
- **The application itself has no Drive credentials.** The backend cannot use this session's connector. It needs its own credentials, configured by the operator (below).

### What the owner needs to provide

1. **Application credentials for Drive** (either one):
   - a Google Cloud **service account** key JSON with the Drive API enabled in its project. Add the service account's email to `eaios-m4-drive-test` as an **Editor**: Drive returns the full permission list only to principals who can share. Or
   - an **OAuth client** (client id and secret) plus a **refresh token** for the folder owner, with scope `drive.readonly`.
2. **A second Google account** to act as the demo reader. You will share the test file with it and later remove it. Sharing with a new address sends that account a Google notification email. The owner's own account cannot be used because an owner permission cannot be revoked.
3. Then run, from the repository root:

```bash
cat > var/connectors/drive.json <<'JSON'
{"tenants": {"niletech": {"auth": {"type": "service_account", "key_file": "/run/eaios/connectors/drive-sa.json"}, "allowed_roots": ["1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L"]}}}
JSON
```

```bash
echo 'DRIVE_CONNECTOR_CONFIG=/run/eaios/connectors/drive.json' >> .env && python3 scripts/dev.py up
```

```bash
docker compose run --rm -v "$PWD/var:/app/var" migrate python -m app.cli add-drive-reader --email <reader Google account>
```

Share `niletech-travel-policy.md` with the reader account (Viewer), then:

```bash
python3 scripts/m4_drive_acceptance.py --folder 1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L --file-id 119GR0k59wCVdBRhQqzhWOpGIuAu9qC_e --reader <reader Google account>
```

The runner works like this:
1. Each sync request returns its own `run_id`, and the runner polls exactly that run. An earlier success or a scheduled run never satisfies the wait.
2. Sync 1, then wait for the test file's document to finish ingestion.
3. Ask as the reader. The answer must cite **that** document, and the citation must open.
4. Pause. Remove the reader from the file's sharing in Drive and press Enter.
5. Sync 2 must succeed with `permissions_changed >= 1`.
6. A new run of the same question and a reworded question must both get `no_sources`.
7. The original citation must return 404, and replaying the original run must end with `access_revoked`.

It always writes `var/m4-drive-acceptance-<UTC>.json`, with a timestamp per step, both run ids and a PASS/FAIL per check. Exit codes: 0 PASS, 1 a check failed, 2 timeout, 3 setup error. Copy the file here. Without a Colab gateway, start the stack with `MODEL_MODE=fake` and say so in the evidence (inference is labelled on every event).

## Recorded runs

### Drive-double revocation rehearsal

[`2026-11-21-drive-double-revocation.json`](2026-11-21-drive-double-revocation.json), produced by:

```bash
cd backend && EAIOS_M4_EVIDENCE=../docs/evidence/m4/2026-11-21-drive-double-revocation.json .venv/bin/pytest -q tests/integration/test_m4_drive_sync.py -k revoked
```

Real PostgreSQL, real Qdrant, the M3 ingestion and answer path, **fake** inference and the **Drive double** (refreshed after the review fixes):

| Step | Time (UTC) | Result |
| --- | --- | --- |
| Sync 1 (first import), run `fee8b700…` | verified at 14:41:36.149 | `created: 1`, access `current` |
| Ask as `finance-reader` | — | `published`, one citation; citation opens (200) |
| Unshare in the Drive double, sync 2, run `a4562540…` | verified at 14:41:37.501 | `permissions_changed: 1` |
| Ask again | — | `abstained`, `no_sources` |
| Reopen citation / replay run | — | `404` / `access_revoked`, no content |

### Compose smoke

[`2026-11-21-compose-smoke.json`](2026-11-21-compose-smoke.json): the laptop Compose stack at migration `0008_sync_fencing` after the re-seed, with Celery dispatch and `MODEL_MODE=fake`. The tenant-wide admin's attempt to remove `identity.admin` from their own role gets `422 cannot_remove_own_admin`. Both tenants report their fixture settings. A tenant-wide admin lists 7 users and a finance admin lists finance users only. A reader gets `403` on admin routes. The isolation tenant's direct `mode=report` request gets `403 capability_disabled`. Creating a source without credentials gets `422 drive_not_configured`. A NileTech document answer is published (fake inference).

## Browser walkthrough

**Done (read-only) on 21 November 2026.** The owner signed in to the local Compose stack (migration 0009, `MODEL_MODE=fake`) in the built-in browser pane, switching between accounts, and I read what the UI showed. I did not type or see any password, and I submitted no admin change.

### Tenant Access Admin (`access.admin@niletech.example`, `access_admin`, tenant-wide)

Identity card: department `it`, capabilities `audit.read, identity.admin, resource.admin, tenant.config`. The Administration card showed all six tabs.

| Tab | What the UI showed | Verified |
| --- | --- | --- |
| Documents | All 11 NileTech documents across finance, hr, it, marketing and sales, with source `upload`, sharing (for example `allow:group:finance-team, deny:user:denied.reader@…`), and access. *Draft strategy memo* shows sharing `nobody` and access `denied` (permissions unknown). The text "Managing a document never lets you read it" is shown | Tenant-wide document scope; sharing and access are metadata only |
| Sources | "No Google Drive sources in your scope" and the add-folder form | Empty state matches no Drive credentials configured. The `drive_not_configured` refusal is **verified by API only** ([Compose smoke](2026-11-21-compose-smoke.json), 422): submitting a probe source in the UI was blocked by the auto-mode classifier and not retried |
| Users | All 7 users across `it`, `finance` and `sales` (including the inactive reader, with an **Activate** button). Roles render as `role` / `role@department` (for example `department_admin@finance`). Group fields are editable | Tenant-wide `identity.admin` scope. No change was submitted |
| Roles & groups | `access_admin`, `department_admin`, `reader`, `report_reader` with their capabilities; groups `all-staff` and `finance-team`, with no linked Google groups | Matches the fixtures |
| Audit | 50 events newest first, from several actors (including `finance.admin`, `sales.reader` and `system`), with `allow`/`info`/`deny` decisions. **Older events** loaded a second page: 100 distinct rows back to 06:09. No document text appeared (probed "370 open invoices", "past their due date", "meal allowance") | Tenant-wide audit scope, paging, no content in audit |
| Organization | documents, sales_reports, upload and google_drive all on; sync "Every 15 minutes (demo)"; controls editable | Tenant-wide `tenant.config` |

### Department admin (`finance.admin@niletech.example`, `department_admin` scoped to finance)

Identity card: department `finance`, capabilities `audit.read (finance), identity.admin (finance), resource.admin (finance)`.

| Check | Observed | Result |
| --- | --- | --- |
| Tabs | Sources, Documents, Users, Roles & groups, Audit. **Organization is absent** | Matches (no `tenant.config`) |
| Documents (admin tab) | 5 rows, all `finance`: Q1 2026 finance commentary, Board minutes, Draft strategy memo (sharing `nobody`, access `denied`), Q1 2026 sales tables, Q1 2026 workbook. The tenant admin saw 11 across five departments | Matches |
| Documents card above the admin panel | The same 5 finance documents and the upload form (the department admin holds `resource.admin` for finance; no `resource.read`) | Matches |
| Users | 5 users, all `finance` (board member, denied reader, finance admin, finance reader, inactive reader). No `it` or `sales` users. Every **group field is disabled**, with no group Save button | Matches (group membership is tenant-wide) |
| Audit | Page 1: 50 events; page 2 (**Older events**): 100 distinct events back to 01:31. Every row is by a finance user (`finance.admin`, `finance.reader`, `inactive.reader`) or concerns a finance resource (the five finance documents or the Sales reporting dataset). `access.admin` appears only on its five `document.upload` rows for finance documents. No `sales.reader` events and none of the tenant admin's own logins, which the tenant admin's view showed. No document text in the audit view | Matches the documented scope (finance resource, finance actor, or `details.department = finance`) |
| Roles & groups | Read-only lists of the four roles and two groups; no edit controls | Matches |
| Sources | Empty state and add form | Matches (refusal verified by API only, as above) |

**Mismatch (presentation only).** In Users, the department admin still sees an editable roles field with **Save** for every user, including tenant-wide assignments such as `reader`, and a **Deactivate** button on their own row. The backend refuses these: changing a tenant-wide assignment returns `403`, and deactivating yourself returns `422` (`test_role_assignment_cannot_escalate`, `test_scoped_admin_cannot_remove_tenant_wide_assignments`, `test_admins_cannot_lock_themselves_out`). I did not press them in the UI. The UI does not hide every control the backend refuses; hiding them is a follow-up.

### Reader (`finance.reader@niletech.example`)

Seen briefly while switching accounts: only Ask, Documents and Your identity; no Administration card and no upload form. This matches a user without admin capabilities.

## Test results

Backend (real PostgreSQL 17.6, Redis 7.4.5 and Qdrant 1.15.4 test containers):

```bash
cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy app tests && .venv/bin/pytest -q
```

| Run | Result |
| --- | --- |
| Full suite (local) | 744 passed, 0 skipped, 0 failed |
| M4 subset (`test_m4_drive_sync.py`, `test_m4_admin.py`, `test_m4_tenant_config.py`, `test_m4_upgrade.py`, `test_drive_client.py`, `test_admin_scope.py`, `test_m4_acceptance_runner.py`), 3 repeats (local) | 102 passed each time, 0 skipped |

The CI results for the final commit are in the PR.

## Not done in M4

- Live Drive run (above).
- Write actions through the admin console UI (the walkthrough was read-only), and hiding controls the backend refuses for department admins (see the walkthrough mismatch).
- Verified Google Groups membership on a real Workspace directory. The code path exists (`group_membership: "directory"`) and is unit-tested against a mocked API only. Without it, Drive group shares deny.
- The Changes API for cheaper incremental listing. Each sync re-reads the tree and every ACL, which is what makes a run "verified" for the small corpus.
