/**
 * Admin console and tenant settings API (M4). The backend enforces every scope and
 * capability; the UI only hides controls the caller cannot use.
 */
import { API_BASE_URL, ApiError, type ApiErrorBody } from "./api";

export interface TenantSettings {
  enabled_packs: string[];
  enabled_connectors: string[];
  sync_mode: "demo" | "nightly";
  sync_interval_seconds: number;
  max_staleness_seconds: number;
  version: number;
  updated_at: string | null;
  available_packs: Record<string, string>;
  available_connectors: Record<string, string>;
}

export interface AdminUser {
  user_id: string;
  email: string;
  display_name: string;
  status: "active" | "inactive";
  clearance: string | null;
  country: string | null;
  department: string | null;
  roles: { role: string; scope_department: string | null }[];
  groups: string[];
}

export interface AdminRole {
  key: string;
  name: string;
  capabilities: string[];
}

export interface AdminGroup {
  key: string;
  name: string;
  google_groups: string[];
}

export interface AdminDocument {
  document_id: string;
  title: string;
  source: string;
  source_name: string | null;
  department: string;
  classification: string;
  permission_state: string;
  permissions_current: boolean;
  status: string | null;
  searchable: boolean;
  current_version: number;
  grants: string[];
  managed_by_connector: boolean;
}

export type SourceAccess =
  | "current"
  | "stale"
  | "last_sync_failed"
  | "resync_required"
  | "never_verified"
  | "paused"
  | "connector_disabled";

export interface SourceItem {
  external_id: string;
  name: string;
  path: string;
  state: string;
  document_id: string | null;
  unresolved_principals: { type: string; email?: string; reason: string }[];
}

export interface AdminSource {
  source_id: string;
  kind: string;
  name: string;
  external_root: string;
  department: string;
  classification: string;
  allowed_countries: string[];
  status: "active" | "paused";
  access: SourceAccess;
  last_verified_sync_at: string | null;
  last_sync_started_at: string | null;
  last_sync_status: string | null;
  last_error_code: string | null;
  sync_mode: string;
  sync_interval_seconds: number;
  max_staleness_seconds: number;
  item_count: number;
  items?: SourceItem[];
  recent_runs?: { run_id: string; status: string; started_at: string; error_code: string | null; stats: Record<string, number> }[];
}

export interface AuditEvent {
  event_id: string;
  occurred_at: string;
  event_type: string;
  action: string;
  decision: string;
  reason: string;
  actor: string | null;
  resource_id: string | null;
  resource_title: string | null;
  resource_department: string | null;
  details: Record<string, unknown>;
}

async function call<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_BASE_URL}/api/v1${path}`, {
    ...init,
    headers,
    credentials: "omit",
    cache: "no-store",
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error =
      body && typeof body === "object" && "error" in body
        ? (body as { error: ApiErrorBody }).error
        : { code: "http_error", message: `Request failed (${response.status})`, request_id: null };
    throw new ApiError(response.status, error);
  }
  return body as T;
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });

export const admin = {
  tenantSettings: (t: string) => call<TenantSettings>(t, "/tenant/settings"),
  saveTenantSettings: (t: string, body: Partial<Pick<TenantSettings, "enabled_packs" | "enabled_connectors" | "sync_mode">>) =>
    call<TenantSettings>(t, "/admin/tenant/settings", json("PUT", body)),
  users: (t: string) => call<AdminUser[]>(t, "/admin/users"),
  updateUser: (t: string, id: string, body: Partial<AdminUser>) =>
    call<AdminUser>(t, `/admin/users/${id}`, json("PATCH", body)),
  setRoles: (t: string, id: string, assignments: { role: string; scope_department: string | null }[]) =>
    call<AdminUser>(t, `/admin/users/${id}/roles`, json("PUT", { assignments })),
  setGroups: (t: string, id: string, groups: string[]) =>
    call<AdminUser>(t, `/admin/users/${id}/groups`, json("PUT", { groups })),
  roles: (t: string) => call<AdminRole[]>(t, "/admin/roles"),
  groups: (t: string) => call<AdminGroup[]>(t, "/admin/groups"),
  setGroupLinks: (t: string, key: string, googleGroups: string[]) =>
    call<AdminGroup>(t, `/admin/groups/${key}/links`, json("PUT", { google_groups: googleGroups })),
  documents: (t: string) => call<AdminDocument[]>(t, "/admin/documents"),
  sources: (t: string) => call<AdminSource[]>(t, "/admin/sources"),
  source: (t: string, id: string) => call<AdminSource>(t, `/admin/sources/${id}`),
  createSource: (t: string, body: Record<string, unknown>) => call<AdminSource>(t, "/admin/sources", json("POST", body)),
  updateSource: (t: string, id: string, body: Record<string, unknown>) =>
    call<AdminSource>(t, `/admin/sources/${id}`, json("PATCH", body)),
  syncSource: (t: string, id: string) =>
    call<{ status: string; error_code: string | null; stats?: Record<string, number> }>(
      t,
      `/admin/sources/${id}/sync`,
      { method: "POST" },
    ),
  audit: (t: string, before?: string) =>
    call<{ events: AuditEvent[]; next_before: string | null }>(
      t,
      `/admin/audit?limit=50${before ? `&before=${encodeURIComponent(before)}` : ""}`,
    ),
};
