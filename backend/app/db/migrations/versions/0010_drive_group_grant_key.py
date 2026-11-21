"""M4 review fixes (3): key ``drive_group_grants`` by the Google group email too.

Two Google groups can be linked to the same internal group. A reader who belongs to
both, on a file shared with both, has two independent reasons for access; each row
must survive the other being unlinked. 0009 keyed rows by (resource, user, internal
group) only, so the second row collided with the first and the sync failed. Rows
written under 0009 are each still valid (one per user and internal group), so they are
kept; the next sync adds the missing per-email rows.

Revision ID: 0010_drive_group_grant_key
Revises: 0009_drive_group_grants
Create Date: 2026-11-21
"""

from alembic import op

revision = "0010_drive_group_grant_key"
down_revision = "0009_drive_group_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        """
        ALTER TABLE drive_group_grants DROP CONSTRAINT drive_group_grants_pkey;
        ALTER TABLE drive_group_grants ADD CONSTRAINT drive_group_grants_pkey
            PRIMARY KEY (tenant_id, resource_id, user_id, group_id, external_email);
        """
    )


def downgrade() -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(
        """
        DELETE FROM drive_group_grants a USING drive_group_grants b
         WHERE a.tenant_id = b.tenant_id AND a.resource_id = b.resource_id
           AND a.user_id = b.user_id AND a.group_id = b.group_id
           AND a.external_email > b.external_email;
        ALTER TABLE drive_group_grants DROP CONSTRAINT drive_group_grants_pkey;
        ALTER TABLE drive_group_grants ADD CONSTRAINT drive_group_grants_pkey
            PRIMARY KEY (tenant_id, resource_id, user_id, group_id);
        """
    )
