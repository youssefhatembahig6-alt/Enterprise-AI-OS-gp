"""M4 review fixes: fence stale Drive syncs and correlate sync requests with runs.

* ``connector_sources.policy_version`` increases whenever an admin changes a source's
  policy (department, classification, restriction, countries, status).
* ``connector_sources.claim_run_id`` names the one sync run that currently holds the
  source. A run may write items, grants and tombstones, mark permissions current or
  release the claim only inside a transaction that first re-locks the source row and
  finds *its own* run id and the policy version it started with. A policy change or a
  newer worker taking over an expired lease therefore fences the older run out.
* ``connector_sync_runs`` gains ``queued`` (created by the API before dispatch, so a
  caller can wait for exactly that run), ``skipped`` (the worker could not claim the
  source) and ``superseded`` (fenced out), plus the policy version it ran under.

Revision ID: 0008_sync_fencing
Revises: 0007_drive_admin_config
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0008_sync_fencing"
down_revision = "0007_drive_admin_config"
branch_labels = None
depends_on = None


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(
        f"""
        ALTER TABLE connector_sources
            ADD COLUMN policy_version bigint NOT NULL DEFAULT 1 CHECK (policy_version >= 1),
            ADD COLUMN claim_run_id uuid;
        ALTER TABLE connector_sync_runs
            ADD COLUMN policy_version bigint,
            ADD COLUMN requested_at timestamptz NOT NULL DEFAULT clock_timestamp();
        ALTER TABLE connector_sync_runs DROP CONSTRAINT connector_sync_runs_status_check;
        ALTER TABLE connector_sync_runs ADD CONSTRAINT connector_sync_runs_status_check
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'skipped',
                              'superseded'));
        GRANT UPDATE (policy_version, claim_run_id) ON connector_sources TO {app};
        GRANT UPDATE (started_at, policy_version) ON connector_sync_runs TO {app};
        """
    )


def downgrade() -> None:
    _run(
        """
        DELETE FROM connector_sync_runs WHERE status IN ('queued', 'skipped', 'superseded');
        ALTER TABLE connector_sync_runs DROP CONSTRAINT connector_sync_runs_status_check;
        ALTER TABLE connector_sync_runs ADD CONSTRAINT connector_sync_runs_status_check
            CHECK (status IN ('running', 'succeeded', 'failed'));
        ALTER TABLE connector_sync_runs DROP COLUMN policy_version, DROP COLUMN requested_at;
        ALTER TABLE connector_sources DROP COLUMN policy_version, DROP COLUMN claim_run_id;
        """
    )
