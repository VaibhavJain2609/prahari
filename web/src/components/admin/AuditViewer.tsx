"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import Panel from "@/components/ui/Panel";

// Verify-only audit surface: there is no audit *browser* endpoint yet, and
// the chain's value is that "is it intact" is a single yes/no question with
// a pointer to the first broken link. Green on ok, red with the broken
// entry id otherwise.
export default function AuditViewer() {
  const [result, setResult] = useState<{ ok: boolean; first_broken_entry: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onVerify() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.verifyAudit());
    } catch (err) {
      setResult(null);
      setError(err instanceof ApiError ? err.message : "verify failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Audit log">
      <div className="flex items-center gap-2">
        <button
          onClick={onVerify}
          disabled={busy}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          {busy ? "Verifying…" : "Verify chain"}
        </button>
        {result?.ok === true && (
          <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
            chain intact
          </span>
        )}
        {result?.ok === false && (
          <span className="text-xs font-medium text-red-600 dark:text-red-400">
            broken at <span className="font-mono">{result.first_broken_entry ?? "?"}</span>
          </span>
        )}
      </div>
      {error && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{error}</p>}
      <p className="mt-2 text-[11px] text-slate-400">
        Walks the hash chain server-side; entries are never listed here — the audit log is
        append-only evidence, not a browse surface.
      </p>
    </Panel>
  );
}
