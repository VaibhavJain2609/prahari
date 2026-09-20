"use client";

import { useState } from "react";
import { api, ApiError, Role } from "@/lib/api";
import { usePrincipal } from "@/lib/principal";
import Panel from "@/components/ui/Panel";

// User creation (was AdminPanel's CreateUser). Writes into the caller's own
// subtree — the BFF's _check_target_org enforces that server-side.
export default function UsersSection() {
  const { principal } = usePrincipal();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [orgId, setOrgId] = useState(principal.org_id);
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

  const input =
    "w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  return (
    <Panel title="Users">
      <form onSubmit={onSubmit} className="space-y-2">
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" aria-label="Username" className={input} />
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password" aria-label="Password" className={input} />
        <input value={orgId} onChange={(e) => setOrgId(e.target.value)} placeholder="org id" aria-label="Org ID" className={`${input} font-mono text-[11px]`} />
        <select value={role} onChange={(e) => setRole(e.target.value as Role)} aria-label="Role" className={input}>
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
    </Panel>
  );
}
