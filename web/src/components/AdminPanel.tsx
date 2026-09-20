"use client";

import { useState } from "react";
import { api, ApiError, ApiKeyPurpose, OrgKind, Principal, Role } from "@/lib/api";

// Sub-org, user and API-key creation. All three write into the caller's own
// subtree — the BFF's _check_target_org enforces that server-side, this
// panel never needs to compute or restrict it client-side, only pass along
// whatever parent_id/org_id the operator typed.
export default function AdminPanel({ principal }: { principal: Principal }) {
  return (
    <div className="space-y-4">
      <CreateOrg parentId={principal.org_id} />
      <CreateUser defaultOrgId={principal.org_id} />
      <CreateApiKey defaultOrgId={principal.org_id} />
    </div>
  );
}

function CreateOrg({ parentId }: { parentId: string }) {
  const [label, setLabel] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<OrgKind>("local_body");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!label.trim() || !name.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      await api.createOrg({ parent_id: parentId, label: label.trim(), kind, name: name.trim() });
      setMessage({ ok: true, text: `${name} created.` });
      setLabel("");
      setName("");
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "create failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">New sub-org</p>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="display name"
        aria-label="Display name"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        value={label}
        onChange={(e) => setLabel(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
        placeholder="path label (e.g. zone_4)"
        aria-label="Path label"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <select
        value={kind}
        onChange={(e) => setKind(e.target.value as OrgKind)}
        aria-label="Org kind"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        <option value="organization">organization</option>
        <option value="local_body">local body</option>
      </select>
      <button
        type="submit"
        disabled={busy || !label.trim() || !name.trim()}
        className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
      >
        {busy ? "Creating…" : "Create org"}
      </button>
      {message && (
        <p className={`text-xs ${message.ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function CreateUser({ defaultOrgId }: { defaultOrgId: string }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [orgId, setOrgId] = useState(defaultOrgId);
  const [role, setRole] = useState<Role>("viewer");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true);
    setMessage(null);
    try {
      await api.createUser({ username: username.trim(), password, org_id: orgId, role });
      setMessage({ ok: true, text: `${username} created.` });
      setUsername("");
      setPassword("");
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "create failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">New user</p>
      <input
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        placeholder="username"
        aria-label="Username"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="password"
        aria-label="Password"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        value={orgId}
        onChange={(e) => setOrgId(e.target.value)}
        placeholder="org id"
        aria-label="Org ID"
        className="w-full rounded border border-slate-300 px-2 py-1 font-mono text-[11px] focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <select
        value={role}
        onChange={(e) => setRole(e.target.value as Role)}
        aria-label="Role"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        <option value="viewer">viewer</option>
        <option value="operator">operator</option>
        <option value="admin">admin</option>
      </select>
      <button
        type="submit"
        disabled={busy || !username.trim() || !password}
        className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
      >
        {busy ? "Creating…" : "Create user"}
      </button>
      {message && (
        <p className={`text-xs ${message.ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function CreateApiKey({ defaultOrgId }: { defaultOrgId: string }) {
  const [orgId, setOrgId] = useState(defaultOrgId);
  const [role, setRole] = useState<Role>("viewer");
  const [purpose, setPurpose] = useState<ApiKeyPurpose>("local_body_registration");
  const [label, setLabel] = useState("");
  const [plaintext, setPlaintext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!label.trim()) return;
    setBusy(true);
    setError(null);
    setPlaintext(null);
    try {
      const res = await api.createApiKey({ org_id: orgId, role, purpose, label: label.trim() });
      setPlaintext(res.plaintext);
      setLabel("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "create failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">New API key</p>
      <input
        value={orgId}
        onChange={(e) => setOrgId(e.target.value)}
        placeholder="org id"
        aria-label="Org ID"
        className="w-full rounded border border-slate-300 px-2 py-1 font-mono text-[11px] focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <select
        value={role}
        onChange={(e) => setRole(e.target.value as Role)}
        aria-label="Role"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        <option value="viewer">viewer</option>
        <option value="operator">operator</option>
        <option value="admin">admin</option>
      </select>
      <select
        value={purpose}
        onChange={(e) => setPurpose(e.target.value as ApiKeyPurpose)}
        aria-label="Key purpose"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        <option value="local_body_registration">local body registration</option>
        <option value="onvif_agent">ONVIF agent</option>
        <option value="vendor_adapter">vendor adapter</option>
        <option value="internal_service">internal service</option>
      </select>
      <input
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        placeholder="label"
        aria-label="Key label"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <button
        type="submit"
        disabled={busy || !label.trim()}
        className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
      >
        {busy ? "Creating…" : "Create key"}
      </button>
      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
      {plaintext && (
        <div className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-[11px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          Shown once — copy it now: <span className="break-all font-mono">{plaintext}</span>
        </div>
      )}
    </form>
  );
}
