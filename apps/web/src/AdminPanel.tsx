import { Alert, Button, Card, Field, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, type Identity } from "./api";
import {
  admin,
  type AdminDocument,
  type AdminRole,
  type AdminSource,
  type AdminUser,
  type AuditEvent,
  type SourceAccess,
  type TenantSettings,
} from "./admin";

type Tab = "sources" | "documents" | "users" | "roles" | "audit" | "organization";

const ACCESS: Record<SourceAccess, { tone: StatusTone; label: string }> = {
  current: { tone: "ok", label: "Permissions current" },
  stale: { tone: "warn", label: "Stale — content denied until next sync" },
  last_sync_failed: { tone: "error", label: "Last sync failed — content denied" },
  resync_required: { tone: "warn", label: "Policy changed — content denied until next sync" },
  never_verified: { tone: "unknown", label: "Not synced yet" },
  paused: { tone: "unknown", label: "Paused — content denied" },
  connector_disabled: { tone: "error", label: "Connector disabled for organization" },
};

function holds(identity: Identity, capability: string, tenantWide = false): boolean {
  return identity.capabilities.some(
    (c) => c.capability === capability && (!tenantWide || c.scope_department === null),
  );
}

function when(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "never";
}

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : "The request failed.";
}

function useLoad<T>(load: () => Promise<T>): [T | null, string | null, () => Promise<void>] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      setData(await load());
      setError(null);
    } catch (err) {
      setError(message(err));
    }
  }, [load]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return [data, error, refresh];
}

function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] text-left text-sm">
        <thead className="text-xs text-muted-fg">
          <tr>
            {head.map((h) => (
              <th key={h} className="py-2 pr-3 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border align-top">{children}</tbody>
      </table>
    </div>
  );
}

// -- sources -------------------------------------------------------------------------

function SourcesTab({ token }: { token: string }) {
  const load = useCallback(() => admin.sources(token), [token]);
  const [sources, error, refresh] = useLoad(load);
  const [open, setOpen] = useState<AdminSource | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", root: "", department: "finance", classification: "internal" });

  const sync = async (id: string) => {
    setNotice("Syncing…");
    try {
      const result = await admin.syncSource(token, id);
      setNotice(
        result.status === "queued"
          ? "Sync queued."
          : `Sync ${result.status}${result.error_code ? ` (${result.error_code})` : ""}.`,
      );
      await refresh();
      if (open?.source_id === id) setOpen(await admin.source(token, id));
    } catch (err) {
      setNotice(message(err));
    }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await admin.createSource(token, {
        name: form.name,
        external_root: form.root.trim(),
        department: form.department,
        classification: form.classification,
        allowed_countries: ["*"],
      });
      setForm({ ...form, name: "", root: "" });
      setNotice("Source added. Run a sync to import it.");
      await refresh();
    } catch (err) {
      setNotice(message(err));
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-fg">
        Drive content is served only while the last verified sync read every file&apos;s sharing and
        is recent enough. Sharing removed in Drive takes effect after the next sync.
      </p>
      {error ? <Alert>{error}</Alert> : null}
      {notice ? <p className="text-sm">{notice}</p> : null}
      {(sources ?? []).length === 0 ? (
        <p className="text-sm text-muted-fg">No Google Drive sources in your scope.</p>
      ) : (
        <ul className="divide-y divide-border text-sm">
          {(sources ?? []).map((s) => (
            <li key={s.source_id} className="flex flex-col gap-2 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{s.name}</span>
                <StatusPill tone={ACCESS[s.access].tone}>{ACCESS[s.access].label}</StatusPill>
                <span className="text-xs text-muted-fg">
                  {s.department} · {s.classification} · {s.item_count} files
                </span>
              </div>
              <div className="text-xs text-muted-fg">
                Last verified sync: <strong>{when(s.last_verified_sync_at)}</strong> · schedule:{" "}
                {s.sync_mode === "demo" ? "every 15 minutes (demo)" : "nightly"}
                {s.last_error_code ? ` · last error: ${s.last_error_code}` : ""}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => void sync(s.source_id)}>
                  Sync now
                </Button>
                <Button
                  variant="secondary"
                  onClick={async () => {
                    await admin.updateSource(token, s.source_id, {
                      status: s.status === "active" ? "paused" : "active",
                    });
                    await refresh();
                  }}
                >
                  {s.status === "active" ? "Pause" : "Resume"}
                </Button>
                <Button
                  variant="secondary"
                  onClick={async () =>
                    setOpen(open?.source_id === s.source_id ? null : await admin.source(token, s.source_id))
                  }
                >
                  {open?.source_id === s.source_id ? "Hide files" : "Files"}
                </Button>
              </div>
              {open?.source_id === s.source_id ? (
                <Table head={["Path", "State", "Not mapped"]}>
                  {(open.items ?? []).map((i) => (
                    <tr key={i.external_id}>
                      <td className="py-2 pr-3 break-all">{i.path}</td>
                      <td className="py-2 pr-3">{i.state.replace("_", " ")}</td>
                      <td className="py-2 pr-3 text-xs text-muted-fg">
                        {i.unresolved_principals
                          .map((p) => `${p.type}${p.email ? ` ${p.email}` : ""} (${p.reason})`)
                          .join("; ") || "—"}
                      </td>
                    </tr>
                  ))}
                </Table>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <form className="grid grid-cols-1 gap-3 sm:grid-cols-2" onSubmit={(e) => void create(e)}>
        <Field id="src-name" label="Source name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <Field id="src-root" label="Drive folder id" value={form.root} onChange={(e) => setForm({ ...form, root: e.target.value })} />
        <Field id="src-dept" label="Department" value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} />
        <div className="flex flex-col gap-1.5">
          <label htmlFor="src-class" className="text-sm font-medium">Classification</label>
          <select
            id="src-class"
            className="h-10 rounded-md border border-border bg-bg px-2 text-sm"
            value={form.classification}
            onChange={(e) => setForm({ ...form, classification: e.target.value })}
          >
            {["public", "internal", "confidential", "restricted"].map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
        <div className="sm:col-span-2">
          <Button type="submit" disabled={!form.name || !form.root}>Add Google Drive folder</Button>
        </div>
      </form>
    </div>
  );
}

// -- documents ----------------------------------------------------------------------------

function DocumentsTab({ token }: { token: string }) {
  const load = useCallback(() => admin.documents(token), [token]);
  const [docs, error] = useLoad<AdminDocument[]>(load);
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-fg">Metadata and sharing only. Managing a document never lets you read it.</p>
      {error ? <Alert>{error}</Alert> : null}
      <Table head={["Title", "Source", "Department", "Sharing", "Access"]}>
        {(docs ?? []).map((d) => (
          <tr key={d.document_id}>
            <td className="py-2 pr-3">
              {d.title} <span className="text-xs text-muted-fg">v{d.current_version}</span>
            </td>
            <td className="py-2 pr-3">{d.source_name ?? d.source}</td>
            <td className="py-2 pr-3">
              {d.department} · {d.classification}
            </td>
            <td className="py-2 pr-3 text-xs break-all">{d.grants.join(", ") || "nobody"}</td>
            <td className="py-2 pr-3">
              <StatusPill tone={d.permissions_current ? "ok" : "warn"}>
                {d.permissions_current ? "current" : "denied"}
              </StatusPill>
            </td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

// -- users and roles ---------------------------------------------------------------------

function UserRow({ token, user, onChange, canGroups }: { token: string; user: AdminUser; onChange: () => Promise<void>; canGroups: boolean }) {
  const [groups, setGroups] = useState(user.groups.join(", "));
  const [roles, setRoles] = useState(
    user.roles.map((r) => (r.scope_department ? `${r.role}@${r.scope_department}` : r.role)).join(", "),
  );
  const [error, setError] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
      await onChange();
    } catch (err) {
      setError(message(err));
    }
  };
  const parsedRoles = () =>
    roles
      .split(",")
      .map((r) => r.trim())
      .filter(Boolean)
      .map((r) => {
        const [role, scope] = r.split("@");
        return { role: role ?? "", scope_department: scope ?? null };
      });
  return (
    <tr>
      <td className="py-2 pr-3">
        <div className="font-medium">{user.display_name}</div>
        <div className="text-xs text-muted-fg break-all">{user.email}</div>
        <div className="text-xs text-muted-fg">
          {user.department ?? "—"} · {user.country ?? "—"} · {user.clearance ?? "—"}
        </div>
        {error ? <div className="text-xs text-error-fg">{error}</div> : null}
      </td>
      <td className="py-2 pr-3">
        <div className="flex gap-2">
          <input aria-label={`Roles of ${user.email}`} className="h-9 w-full min-w-40 rounded-md border border-border bg-bg px-2 text-xs"
                 value={roles} onChange={(e) => setRoles(e.target.value)} />
          <Button variant="secondary" onClick={() => void act(() => admin.setRoles(token, user.user_id, parsedRoles()))}>Save</Button>
        </div>
      </td>
      <td className="py-2 pr-3">
        <div className="flex gap-2">
          <input aria-label={`Groups of ${user.email}`} disabled={!canGroups}
                 className="h-9 w-full min-w-32 rounded-md border border-border bg-bg px-2 text-xs disabled:opacity-60"
                 value={groups} onChange={(e) => setGroups(e.target.value)} />
          {canGroups ? (
            <Button variant="secondary" onClick={() => void act(() => admin.setGroups(token, user.user_id, groups.split(",").map((g) => g.trim()).filter(Boolean)))}>
              Save
            </Button>
          ) : null}
        </div>
      </td>
      <td className="py-2 pr-3">
        <Button
          variant="secondary"
          onClick={() => void act(() => admin.updateUser(token, user.user_id, { status: user.status === "active" ? "inactive" : "active" }))}
        >
          {user.status === "active" ? "Deactivate" : "Activate"}
        </Button>
      </td>
    </tr>
  );
}

function UsersTab({ token, canGroups }: { token: string; canGroups: boolean }) {
  const load = useCallback(() => admin.users(token), [token]);
  const [users, error, refresh] = useLoad(load);
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-fg">
        Changes apply to the next request and to answers still being prepared. Roles are written
        as <code>role</code> or <code>role@department</code>.
      </p>
      {error ? <Alert>{error}</Alert> : null}
      <Table head={["User", "Roles", "Groups", "Status"]}>
        {(users ?? []).map((u) => (
          <UserRow key={`${u.user_id}:${u.roles.length}:${u.groups.length}:${u.status}`} token={token} user={u} onChange={refresh} canGroups={canGroups} />
        ))}
      </Table>
    </div>
  );
}

function RolesTab({ token }: { token: string }) {
  const loadRoles = useCallback(() => admin.roles(token), [token]);
  const loadGroups = useCallback(() => admin.groups(token), [token]);
  const [roles, error] = useLoad<AdminRole[]>(loadRoles);
  const [groups] = useLoad(loadGroups);
  return (
    <div className="flex flex-col gap-4">
      {error ? <Alert>{error}</Alert> : null}
      <Table head={["Role", "Capabilities"]}>
        {(roles ?? []).map((r) => (
          <tr key={r.key}>
            <td className="py-2 pr-3">{r.name} <span className="text-xs text-muted-fg">({r.key})</span></td>
            <td className="py-2 pr-3 text-xs">{r.capabilities.join(", ") || "none"}</td>
          </tr>
        ))}
      </Table>
      <Table head={["Group", "Linked Google groups"]}>
        {(groups ?? []).map((g) => (
          <tr key={g.key}>
            <td className="py-2 pr-3">{g.name} <span className="text-xs text-muted-fg">({g.key})</span></td>
            <td className="py-2 pr-3 text-xs break-all">{g.google_groups.join(", ") || "—"}</td>
          </tr>
        ))}
      </Table>
    </div>
  );
}

// -- audit and organization ----------------------------------------------------------------

function AuditTab({ token }: { token: string }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const page = useCallback(
    async (before?: string) => {
      try {
        const result = await admin.audit(token, before);
        setEvents((prev) => (before ? [...prev, ...result.events] : result.events));
        setNext(result.next_before);
      } catch (err) {
        setError(message(err));
      }
    },
    [token],
  );
  useEffect(() => {
    void page();
  }, [page]);
  return (
    <div className="flex flex-col gap-3">
      {error ? <Alert>{error}</Alert> : null}
      <Table head={["When", "Event", "Actor", "Resource", "Decision"]}>
        {events.map((e) => (
          <tr key={e.event_id}>
            <td className="py-2 pr-3 text-xs whitespace-nowrap">{when(e.occurred_at)}</td>
            <td className="py-2 pr-3 text-xs">{e.event_type}<div className="text-muted-fg">{e.reason}</div></td>
            <td className="py-2 pr-3 text-xs break-all">{e.actor ?? "system"}</td>
            <td className="py-2 pr-3 text-xs">{e.resource_title ?? "—"}</td>
            <td className="py-2 pr-3 text-xs">{e.decision}</td>
          </tr>
        ))}
      </Table>
      {next ? <div><Button variant="secondary" onClick={() => void page(next)}>Older events</Button></div> : null}
    </div>
  );
}

function OrganizationTab({ token, canEdit, onSaved }: { token: string; canEdit: boolean; onSaved: (s: TenantSettings) => void }) {
  const load = useCallback(() => admin.tenantSettings(token), [token]);
  const [settings, error, refresh] = useLoad(load);
  const [notice, setNotice] = useState<string | null>(null);
  if (!settings) return error ? <Alert>{error}</Alert> : null;
  const toggle = async (field: "enabled_packs" | "enabled_connectors", key: string) => {
    const current = new Set(settings[field]);
    if (current.has(key)) current.delete(key);
    else current.add(key);
    try {
      onSaved(await admin.saveTenantSettings(token, { [field]: [...current] }));
      setNotice("Saved. The change applies to the next request.");
      await refresh();
    } catch (err) {
      setNotice(message(err));
    }
  };
  const section = (title: string, field: "enabled_packs" | "enabled_connectors", options: Record<string, string>) => (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-sm font-medium">{title}</legend>
      {Object.entries(options).map(([key, label]) => (
        <label key={key} className="flex items-center gap-2 text-sm">
          <input type="checkbox" disabled={!canEdit} checked={settings[field].includes(key)} onChange={() => void toggle(field, key)} />
          {label} <span className="text-xs text-muted-fg">({key})</span>
        </label>
      ))}
    </fieldset>
  );
  return (
    <div className="flex flex-col gap-4">
      {section("Packs", "enabled_packs", settings.available_packs)}
      {section("Connectors", "enabled_connectors", settings.available_connectors)}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label htmlFor="sync-mode" className="font-medium">Drive sync</label>
        <select
          id="sync-mode"
          disabled={!canEdit}
          className="h-9 rounded-md border border-border bg-bg px-2"
          value={settings.sync_mode}
          onChange={async (e) => {
            onSaved(await admin.saveTenantSettings(token, { sync_mode: e.target.value as "demo" | "nightly" }));
            await refresh();
          }}
        >
          <option value="demo">Every 15 minutes (demo)</option>
          <option value="nightly">Nightly</option>
        </select>
        <span className="text-xs text-muted-fg">Content is denied once the last verified sync is older than {Math.round(settings.max_staleness_seconds / 60)} minutes.</span>
      </div>
      {!canEdit ? <p className="text-xs text-muted-fg">Only a tenant-wide configuration administrator can change these.</p> : null}
      {notice ? <p className="text-sm">{notice}</p> : null}
    </div>
  );
}

export function AdminPanel({ token, identity, settings, onSettings }: { token: string; identity: Identity; settings: TenantSettings | null; onSettings: (s: TenantSettings) => void }) {
  const tabs: { key: Tab; label: string; show: boolean }[] = [
    { key: "sources", label: "Sources", show: holds(identity, "resource.admin") && Boolean(settings?.enabled_connectors.includes("google_drive")) },
    { key: "documents", label: "Documents", show: holds(identity, "resource.admin") },
    { key: "users", label: "Users", show: holds(identity, "identity.admin") },
    { key: "roles", label: "Roles & groups", show: holds(identity, "identity.admin") },
    { key: "audit", label: "Audit", show: holds(identity, "audit.read") },
    { key: "organization", label: "Organization", show: holds(identity, "tenant.config", true) },
  ];
  const visible = tabs.filter((t) => t.show);
  const [tab, setTab] = useState<Tab | null>(visible[0]?.key ?? null);
  if (visible.length === 0 || tab === null) return null;
  return (
    <Card title="Administration">
      <div className="mb-4 flex flex-wrap gap-2" role="tablist">
        {visible.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            className={`h-9 rounded-md px-3 text-sm ${tab === t.key ? "bg-primary text-primary-fg" : "border border-border hover:bg-muted"}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "sources" ? <SourcesTab token={token} /> : null}
      {tab === "documents" ? <DocumentsTab token={token} /> : null}
      {tab === "users" ? <UsersTab token={token} canGroups={holds(identity, "identity.admin", true)} /> : null}
      {tab === "roles" ? <RolesTab token={token} /> : null}
      {tab === "audit" ? <AuditTab token={token} /> : null}
      {tab === "organization" ? <OrganizationTab token={token} canEdit={holds(identity, "tenant.config", true)} onSaved={onSettings} /> : null}
    </Card>
  );
}
