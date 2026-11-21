import { Alert, Button, Card, Field, StatusPill, type StatusTone } from "@eaios/ui";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { admin, type TenantSettings } from "./admin";
import { AdminPanel } from "./AdminPanel";
import { api, ApiError, type Identity } from "./api";
import { DocumentsPanel } from "./DocumentsPanel";
import { ReportPanel } from "./ReportPanel";
import { useApiStatus, type ApiStatus } from "./useApiStatus";

interface Session {
  token: string; // memory only
  identity: Identity;
}

function statusView(status: ApiStatus): { tone: StatusTone; label: string; detail?: string } {
  switch (status.kind) {
    case "checking":
      return { tone: "unknown", label: "Checking API…" };
    case "unreachable":
      return { tone: "error", label: "API unreachable", detail: "The backend is not responding." };
    case "degraded": {
      const failing = Object.entries(status.readiness.checks)
        .filter(([, v]) => v !== "ok")
        .map(([k]) => k);
      return {
        tone: "warn",
        label: "API degraded",
        detail: `Unavailable: ${failing.join(", ") || "unknown"}`,
      };
    }
    case "ready":
      return { tone: "ok", label: "API ready" };
  }
}

function ApiStatusBar({ status }: { status: ApiStatus }) {
  const view = statusView(status);
  const model =
    status.kind === "ready" || status.kind === "degraded" ? status.readiness.model_inference : null;
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <StatusPill tone={view.tone}>{view.label}</StatusPill>
      {view.detail ? <span className="text-muted-fg">{view.detail}</span> : null}
      {model ? (
        <span className="text-muted-fg">
          Model inference:{" "}
          {model === "not_configured"
            ? "not configured"
            : model === "fake"
              ? "FAKE (development double, not a model)"
              : "external (unchecked)"}
        </span>
      ) : null}
    </div>
  );
}

function SignInForm({ disabled, onSignedIn }: { disabled: boolean; onSignedIn: (s: Session) => void }) {
  const [tenant, setTenant] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { access_token } = await api.login(tenant.trim(), email.trim(), password);
      const identity = await api.me(access_token);
      setPassword("");
      onSignedIn({ token: access_token, identity });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.code === "rate_limited"
            ? "Too many failed attempts. Please wait and try again."
            : err.code === "invalid_credentials"
              ? "Sign-in failed. Check your organization, email and password."
              : err.message,
        );
      } else {
        setError("Could not reach the API.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Sign in">
      <form className="flex flex-col gap-4" onSubmit={(e) => void submit(e)} noValidate>
        <Field
          id="tenant"
          label="Organization"
          autoComplete="organization"
          placeholder="organization-slug"
          value={tenant}
          onChange={(e) => setTenant(e.target.value)}
          required
        />
        <Field
          id="email"
          label="Email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <Field
          id="password"
          label="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error ? <Alert>{error}</Alert> : null}
        <Button type="submit" disabled={disabled || busy || !tenant || !email || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-3">
      <dt className="text-sm text-muted-fg">{label}</dt>
      <dd className="text-sm break-words sm:col-span-2">{value}</dd>
    </div>
  );
}

function IdentityView({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const { identity } = session;
  const caps = identity.capabilities
    .map((c) => (c.scope_department ? `${c.capability} (${c.scope_department})` : c.capability))
    .join(", ");
  return (
    <Card title="Your identity">
      <p className="mb-4 text-sm text-muted-fg">
        These details come from the server for your current session. They cannot be changed from
        the browser.
      </p>
      <dl className="divide-y divide-border">
        <Row label="Name" value={identity.display_name} />
        <Row label="Email" value={identity.email} />
        <Row label="Organization" value={`${identity.tenant.name} (${identity.tenant.slug})`} />
        <Row label="Roles" value={identity.roles.join(", ") || "none"} />
        <Row label="Capabilities" value={caps || "none"} />
        <Row label="Groups" value={identity.groups.join(", ") || "none"} />
        <Row label="Department" value={identity.attributes.department ?? "not set"} />
        <Row label="Country" value={identity.attributes.country ?? "not set"} />
        <Row label="Clearance" value={identity.attributes.clearance ?? "not set"} />
        <Row label="Policy version" value={String(identity.policy_epoch)} />
        <Row
          label="Session expires"
          value={new Date(identity.session_expires_at).toLocaleString()}
        />
      </dl>
      <div className="mt-6 flex justify-end">
        <Button variant="secondary" onClick={onSignOut}>
          Sign out
        </Button>
      </div>
    </Card>
  );
}

export function App() {
  const status = useApiStatus();
  const [session, setSession] = useState<Session | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [settings, setSettings] = useState<TenantSettings | null>(null);

  useEffect(() => {
    if (!session) {
      setSettings(null);
      return;
    }
    admin.tenantSettings(session.token).then(setSettings, () => setSettings(null));
  }, [session]);

  const signOut = useCallback(async () => {
    const current = session;
    setSession(null);
    if (!current) return;
    try {
      await api.logout(current.token);
      setNotice("You have signed out.");
    } catch {
      setNotice("Signed out locally. The server session will expire on its own.");
    }
  }, [session]);

  return (
    <main
      className={`mx-auto flex min-h-screen w-full flex-col gap-6 px-4 py-10 ${session ? "max-w-3xl" : "max-w-xl"}`}
    >
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Enterprise AI OS</h1>
        <ApiStatusBar status={status} />
      </header>
      {notice && !session ? <p className="text-sm text-muted-fg">{notice}</p> : null}
      {session ? (
        <>
          <ReportPanel token={session.token} packs={settings?.enabled_packs ?? null} />
          <DocumentsPanel
            token={session.token}
            canUpload={
              session.identity.capabilities.some((c) => c.capability === "resource.admin") &&
              Boolean(settings?.enabled_connectors.includes("upload"))
            }
          />
          <AdminPanel
            token={session.token}
            identity={session.identity}
            settings={settings}
            onSettings={setSettings}
          />
          <IdentityView session={session} onSignOut={() => void signOut()} />
        </>
      ) : (
        <SignInForm
          disabled={status.kind === "unreachable"}
          onSignedIn={(s) => {
            setNotice(null);
            setSession(s);
          }}
        />
      )}
    </main>
  );
}
