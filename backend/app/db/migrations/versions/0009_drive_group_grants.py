"""M4 review fixes (2): group-derived Drive grants that depend on live membership,
per-attempt sync claims, and a fail-closed upgrade.

* ``drive_group_grants`` records "user U was a verified member of Google group E, which
  was linked to internal group G, at the last sync". It is **not** a grant on its own:
  ``load_resource_meta(s)`` turns a row into an allow only while the link E -> G still
  exists and U is still in G. Removing the internal membership or the link therefore
  takes effect at the next authorization point, without a Drive sync. Changes to these
  rows and to ``external_group_links`` raise the policy epoch (publication recheck).
* ``connector_sources.claim_token`` identifies one *attempt* of a run (a redelivered
  manual run that recovers an expired lease gets a new token), and
  ``connector_sync_runs.attempt`` lets a superseded attempt update only its own row.
* **Upgrade fails closed.** Databases at 0007 may hold Drive group grants created
  without Google membership verification; databases at 0008 hold group-derived access
  as plain user grants that do not follow internal membership. Neither can be told
  apart from valid grants, so every connector source is marked not current
  (``last_error_code = 'upgrade_resync_required'``), group grants on connector
  resources are removed, and item fingerprints are cleared so the next sync rewrites
  every item under the new rules. Drive content stays denied until that sync. M3
  upload resources (``source_id IS NULL``) are untouched.

Revision ID: 0009_drive_group_grants
Revises: 0008_sync_fencing
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0009_drive_group_grants"
down_revision = "0008_sync_fencing"
branch_labels = None
depends_on = None


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(
        f"""
        CREATE TABLE drive_group_grants (
            tenant_id       uuid NOT NULL,
            resource_id     uuid NOT NULL,
            user_id         uuid NOT NULL,
            group_id        uuid NOT NULL,
            external_email  text NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, resource_id, user_id, group_id),
            FOREIGN KEY (tenant_id, resource_id) REFERENCES resources (tenant_id, id)
                ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id) ON DELETE CASCADE
        );
        CREATE INDEX drive_group_grants_resource_idx ON drive_group_grants (tenant_id, resource_id);
        ALTER TABLE drive_group_grants ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON drive_group_grants
            USING (tenant_id = app_current_tenant_id())
            WITH CHECK (tenant_id = app_current_tenant_id());
        CREATE TRIGGER drive_group_grants_policy_epoch
            AFTER INSERT OR UPDATE OR DELETE ON drive_group_grants
            FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();
        CREATE TRIGGER external_group_links_policy_epoch
            AFTER INSERT OR UPDATE OR DELETE ON external_group_links
            FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();

        ALTER TABLE connector_sources ADD COLUMN claim_token uuid;
        ALTER TABLE connector_sync_runs ADD COLUMN attempt integer NOT NULL DEFAULT 1
            CHECK (attempt >= 1);

        -- Fail closed: nothing synced before this revision is trusted until resynced.
        DELETE FROM resource_grants g USING resources r
         WHERE r.tenant_id = g.tenant_id AND r.id = g.resource_id
           AND r.source_id IS NOT NULL AND g.group_id IS NOT NULL;
        UPDATE connector_sources
           SET permissions_current = false, last_error_code = 'upgrade_resync_required',
               last_sync_status = NULL, claim_run_id = NULL, sync_claimed_until = NULL,
               policy_version = policy_version + 1, updated_at = now();
        UPDATE drive_items SET permissions_fingerprint = NULL;

        GRANT SELECT, INSERT, DELETE ON drive_group_grants TO {app};
        GRANT UPDATE (claim_token) ON connector_sources TO {app};
        GRANT UPDATE (attempt) ON connector_sync_runs TO {app};
        """
    )


def downgrade() -> None:
    _run(
        """
        DROP TRIGGER IF EXISTS external_group_links_policy_epoch ON external_group_links;
        DROP TABLE IF EXISTS drive_group_grants;
        ALTER TABLE connector_sources DROP COLUMN IF EXISTS claim_token;
        ALTER TABLE connector_sync_runs DROP COLUMN IF EXISTS attempt;
        UPDATE connector_sources SET permissions_current = false;
        """
    )
