"""M4: Google Drive sources and sync state, scoped administration, tenant configuration.

Invariants (docs/adr/0401..0403):

* **Tenant configuration** (``tenant_settings``) lists the enabled packs and
  connectors and the sync schedule. A tenant without a row has nothing enabled. A
  change raises the policy epoch, so in-flight runs re-check it at publication.
* **Drive permissions are only as current as the last verified sync.** A resource
  imported from a connector source is readable only while the source is active, its
  connector is enabled for the tenant, its last sync verified every item's
  permissions (``permissions_current``) and that verification is younger than the
  tenant's ``max_staleness_seconds``. Anything else denies content (fail closed).
  Metadata administration is unaffected.
* **Drive items** map a Drive file id to the protected resource created for it, with
  content and permission fingerprints so unchanged files are not re-ingested and
  unchanged ACLs do not churn the policy epoch. Removed items keep a row
  (``state = 'removed'``) and their document keeps its tombstone.
* **Scoped administration**: new capabilities ``identity.admin`` (users, roles,
  group memberships), ``audit.read`` (audit activity) and ``tenant.config`` (packs,
  connectors). Scope is the existing ``user_roles.scope_department``.
* Audit events become readable by the application role inside the tenant only; the
  API filters further by the caller's ``audit.read`` scope.

Revision ID: 0007_drive_admin_config
Revises: 0006_document_elements
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0007_drive_admin_config"
down_revision = "0006_document_elements"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = (
    "tenant_settings",
    "connector_sources",
    "connector_sync_runs",
    "drive_items",
    "external_group_links",
)

SCHEMA = r"""
ALTER TABLE role_capabilities DROP CONSTRAINT role_capabilities_capability_check;
ALTER TABLE role_capabilities ADD CONSTRAINT role_capabilities_capability_check
    CHECK (capability IN ('resource.read', 'resource.admin', 'dataset.query',
                          'identity.admin', 'audit.read', 'tenant.config'));

ALTER TABLE documents DROP CONSTRAINT documents_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_source_check
    CHECK (source IN ('upload', 'fixture', 'google_drive'));

-- Packs and connectors are code; this row only switches them on per tenant.
CREATE TABLE tenant_settings (
    tenant_id              uuid PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    enabled_packs          text[] NOT NULL DEFAULT '{}'
                           CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports']::text[]),
    enabled_connectors     text[] NOT NULL DEFAULT '{}'
                           CHECK (enabled_connectors <@ ARRAY['upload', 'google_drive']::text[]),
    sync_mode              text NOT NULL DEFAULT 'nightly' CHECK (sync_mode IN ('demo', 'nightly')),
    -- demo: every 15 minutes; nightly: once a day after 02:00 UTC.
    sync_interval_seconds  integer GENERATED ALWAYS AS
                           (CASE sync_mode WHEN 'demo' THEN 900 ELSE 86400 END) STORED,
    -- How old the last verified sync may be before connector content is denied:
    -- one missed demo sync (30 min) or a nightly run plus six hours (30 h).
    max_staleness_seconds  integer GENERATED ALWAYS AS
                           (CASE sync_mode WHEN 'demo' THEN 1800 ELSE 108000 END) STORED,
    version                integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_by             uuid,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE connector_sources (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    kind                   text NOT NULL CHECK (kind IN ('google_drive')),
    name                   text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    external_root          text NOT NULL CHECK (external_root ~ '^[A-Za-z0-9_-]{5,200}$'),
    -- Attributes Drive does not carry; the admin sets them for the whole source.
    department             text NOT NULL CHECK (department ~ '^[a-z][a-z0-9_-]{0,62}$'),
    classification         text NOT NULL
                           CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    department_restricted  boolean NOT NULL DEFAULT false,
    allowed_countries      text[] NOT NULL CHECK (cardinality(allowed_countries) >= 1),
    status                 text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'deleted')),
    permissions_current    boolean NOT NULL DEFAULT false,
    last_sync_started_at   timestamptz,
    last_sync_finished_at  timestamptz,
    last_verified_sync_at  timestamptz,
    last_sync_status       text CHECK (last_sync_status IN ('running', 'succeeded', 'failed')),
    last_error_code        text,
    sync_claimed_until     timestamptz,
    created_by             uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, kind, external_root)
);

CREATE TABLE connector_sync_runs (
    id            uuid PRIMARY KEY,
    tenant_id     uuid NOT NULL,
    source_id     uuid NOT NULL,
    trigger       text NOT NULL CHECK (trigger IN ('scheduled', 'manual')),
    requested_by  uuid,
    status        text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at   timestamptz,
    error_code    text,
    stats         jsonb NOT NULL DEFAULT '{}'::jsonb,
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX connector_sync_runs_source_idx ON connector_sync_runs (tenant_id, source_id, started_at);

CREATE TABLE drive_items (
    tenant_id                uuid NOT NULL,
    source_id                uuid NOT NULL,
    external_id              text NOT NULL CHECK (external_id ~ '^[A-Za-z0-9_-]{5,200}$'),
    document_id              uuid,
    name                     text NOT NULL,
    mime_type                text NOT NULL,
    parent_external_id       text,
    path                     text NOT NULL,
    content_fingerprint      text,
    permissions_fingerprint  text,
    unresolved_principals    jsonb NOT NULL DEFAULT '[]'::jsonb,
    state                    text NOT NULL CHECK (state IN
                             ('active', 'removed', 'unsupported', 'permissions_unknown')),
    last_seen_run            uuid,
    first_seen_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
    removed_at               timestamptz,
    PRIMARY KEY (tenant_id, source_id, external_id),
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
);
CREATE UNIQUE INDEX drive_items_document_idx ON drive_items (tenant_id, document_id)
    WHERE document_id IS NOT NULL;

-- A Drive group permission grants an internal group only through an explicit link.
-- Membership of the internal group is managed in this application (Google Groups
-- membership needs a Workspace directory API that consumer accounts do not have).
CREATE TABLE external_group_links (
    tenant_id       uuid NOT NULL,
    provider        text NOT NULL CHECK (provider IN ('google')),
    external_email  text NOT NULL CHECK (external_email = lower(external_email)
                                         AND position('@' in external_email) > 1),
    group_id        uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider, external_email),
    FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id) ON DELETE CASCADE
);

ALTER TABLE resources ADD COLUMN source_id uuid;
ALTER TABLE resources ADD CONSTRAINT resources_source_fk
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id);
CREATE INDEX resources_source_idx ON resources (tenant_id, source_id) WHERE source_id IS NOT NULL;

-- Existing tenants keep today's behaviour; new tenants start with nothing enabled.
INSERT INTO tenant_settings (tenant_id, enabled_packs, enabled_connectors, sync_mode)
SELECT id, ARRAY['documents', 'sales_reports'], ARRAY['upload'], 'nightly' FROM tenants;

CREATE TRIGGER tenant_settings_policy_epoch
    AFTER INSERT OR UPDATE OF enabled_packs, enabled_connectors, sync_mode OR DELETE
    ON tenant_settings FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();
-- Each sync writes permissions_current; only a real change alters authorization.
CREATE TRIGGER connector_sources_policy_epoch_update
    AFTER UPDATE OF status, permissions_current, department, classification,
        department_restricted, allowed_countries ON connector_sources FOR EACH ROW
    WHEN ((OLD.status, OLD.permissions_current, OLD.department, OLD.classification,
           OLD.department_restricted, OLD.allowed_countries)
          IS DISTINCT FROM
          (NEW.status, NEW.permissions_current, NEW.department, NEW.classification,
           NEW.department_restricted, NEW.allowed_countries))
    EXECUTE FUNCTION bump_policy_epoch();
CREATE TRIGGER connector_sources_policy_epoch_change
    AFTER INSERT OR DELETE ON connector_sources
    FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();

-- Scheduler: sources whose next sync is due. demo: 15 minutes after the last start;
-- nightly: once per day after 02:00 UTC. Expired claims are due again.
CREATE FUNCTION connector_sources_due(p_limit integer)
    RETURNS TABLE (source_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT s.id, s.tenant_id
      FROM public.connector_sources s
      JOIN public.tenant_settings ts ON ts.tenant_id = s.tenant_id
      JOIN public.tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.status = 'active'
       AND s.kind = ANY (ts.enabled_connectors)
       AND (s.sync_claimed_until IS NULL OR s.sync_claimed_until < clock_timestamp())
       AND (s.last_sync_started_at IS NULL
            OR (ts.sync_mode = 'demo'
                AND s.last_sync_started_at
                    < clock_timestamp() - make_interval(secs => ts.sync_interval_seconds))
            OR (ts.sync_mode = 'nightly'
                AND s.last_sync_started_at < (
                    CASE WHEN clock_timestamp() >= date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                                   AT TIME ZONE 'UTC' + interval '2 hours'
                         THEN date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC' + interval '2 hours'
                         ELSE date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC' - interval '22 hours'
                    END)))
     ORDER BY s.last_sync_started_at NULLS FIRST
     LIMIT least(greatest(p_limit, 0), 50)
$$;
REVOKE ALL ON FUNCTION connector_sources_due(integer) FROM PUBLIC;

-- Audit activity becomes readable inside the caller's tenant (scope filtered by the API).
CREATE POLICY audit_select ON audit_events FOR SELECT
    USING (tenant_id = app_current_tenant_id());
CREATE INDEX audit_events_resource_idx ON audit_events (tenant_id, resource_id, occurred_at);
"""


def _grants(app: str) -> str:
    return f"""
GRANT SELECT ON audit_events TO {app};
GRANT SELECT, INSERT ON tenant_settings TO {app};
GRANT UPDATE (enabled_packs, enabled_connectors, sync_mode, version, updated_by, updated_at)
    ON tenant_settings TO {app};
GRANT SELECT, INSERT ON connector_sources TO {app};
GRANT UPDATE (name, department, classification, department_restricted, allowed_countries, status,
              permissions_current, last_sync_started_at, last_sync_finished_at,
              last_verified_sync_at, last_sync_status, last_error_code, sync_claimed_until,
              updated_at)
    ON connector_sources TO {app};
GRANT SELECT, INSERT ON connector_sync_runs TO {app};
GRANT UPDATE (status, finished_at, error_code, stats) ON connector_sync_runs TO {app};
GRANT SELECT, INSERT ON drive_items TO {app};
GRANT UPDATE (document_id, name, mime_type, parent_external_id, path, content_fingerprint,
              permissions_fingerprint, unresolved_principals, state, last_seen_run, updated_at,
              removed_at)
    ON drive_items TO {app};
GRANT SELECT, INSERT, DELETE ON external_group_links TO {app};
GRANT UPDATE (group_id) ON external_group_links TO {app};
GRANT UPDATE (source_id) ON resources TO {app};
GRANT EXECUTE ON FUNCTION connector_sources_due(integer) TO {app};
"""


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(SCHEMA)
    for table in NEW_TENANT_TABLES:
        _run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _run(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
    _run(_grants(app))


def downgrade() -> None:
    _run(
        """
        DROP POLICY IF EXISTS audit_select ON audit_events;
        DROP INDEX IF EXISTS audit_events_resource_idx;
        DROP FUNCTION IF EXISTS connector_sources_due(integer);
        ALTER TABLE resources DROP CONSTRAINT IF EXISTS resources_source_fk;
        ALTER TABLE resources DROP COLUMN IF EXISTS source_id;
        DROP TABLE IF EXISTS external_group_links, drive_items, connector_sync_runs,
            connector_sources, tenant_settings CASCADE;
        DELETE FROM documents WHERE source = 'google_drive';
        ALTER TABLE documents DROP CONSTRAINT documents_source_check;
        ALTER TABLE documents ADD CONSTRAINT documents_source_check
            CHECK (source IN ('upload', 'fixture'));
        DELETE FROM role_capabilities
         WHERE capability IN ('identity.admin', 'audit.read', 'tenant.config');
        ALTER TABLE role_capabilities DROP CONSTRAINT role_capabilities_capability_check;
        ALTER TABLE role_capabilities ADD CONSTRAINT role_capabilities_capability_check
            CHECK (capability IN ('resource.read', 'resource.admin', 'dataset.query'));
        """
    )
