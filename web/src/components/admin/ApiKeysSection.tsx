"use client";

import { useState } from "react";
import { api, ApiError, ApiKeyPurpose, Role } from "@/lib/api";
import { usePrincipal } from "@/lib/principal";
import Panel from "@/components/ui/Panel";

// API-key creation (was AdminPanel's CreateApiKey). The plaintext is shown
// exactly once — the BFF stores only a hash — so the UI says so plainly.
export default function ApiKeysSection() {
  const { principal } = usePrincipal();
  const [orgId, setOrgId] = useState(principal.org_id);
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

  const input =
    "w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  return (
    <Panel title="API keys">
      <form onSubmit={onSubmit} className="space-y-2">
        <input value={orgId} onChange={(e) => setOrgId(e.target.value)} placeholder="org id" aria-label="Org ID" className={`${input} font-mono text-[11px]`} />
        <select value={role} onChange={(e) => setRole(e.target.value as Role)} aria-label="Role" className={input}>
          <option value="viewer">viewer</option>
          <option value="operator">operator</option>
          <option value="admin">admin</option>
        </select>
        <select value={purpose} onChange={(e) => setPurpose(e.target.value as ApiKeyPurpose)} aria-label="Key purpose" className={input}>
          <option value="local_body_registration">local body registration</option>
          <option value="onvif_agent">ONVIF agent</option>
          <option value="vendor_adapter">vendor adapter</option>
          <option value="internal_service">internal service</option>
        </select>
        <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="label" aria-label="Key label" className={input} />
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
    </Panel>
  );
}
