"use client";

import { useState } from "react";
import { api, ApiError, AuditEntry } from "@/lib/api";
import Panel from "@/components/ui/Panel";
import { timeAgo } from "@/lib/format";

// The audit surface: `verify` answers "is the chain intact" (a yes/no with
// a pointer to the first broken link) and the entry list answers "what was
// recorded" — both admin-only reads of the same append-only log. The list
// fetches on demand, not on mount: audit reads are themselves the kind of
// access the log exists to record, so they stay an explicit act.
export default function AuditViewer() {
  const [result, setResult] = useState<{ ok: boolean; first_broken_entry: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [listBusy, setListBusy] = useState(false);
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);
  const PAGE = 50;

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

  async function load(nextOffset: number) {
    setListBusy(true);
    setListError(null);
    try {
      const rows = await api.listAudit({
        limit: PAGE,
        offset: nextOffset,
        actor: actor || undefined,
        action: action || undefined,
      });
      setEntries(nextOffset === 0 ? rows : [...(entries ?? []), ...rows]);
      setOffset(nextOffset + rows.length);
    } catch (err) {
      setListError(err instanceof ApiError ? err.message : "load failed");
    } finally {
      setListBusy(false);
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

      <div className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
        <div className="flex items-center gap-1.5">
          <input
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            placeholder="actor contains…"
            aria-label="Filter by actor"
            className="w-32 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800"
          />
          <input
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="action contains…"
            aria-label="Filter by action"
            className="w-32 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800"
          />
          <button
            onClick={() => load(0)}
            disabled={listBusy}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            {listBusy ? "Loading…" : entries ? "Refresh" : "Browse entries"}
          </button>
        </div>
        {listError && (
          <p className="mt-1 text-xs text-red-600 dark:text-red-400">{listError}</p>
        )}
        {entries && (
          <>
            <ul className="mt-2 max-h-52 space-y-0.5 overflow-y-auto text-xs">
              {entries.map((e) => (
                <li
                  key={e.id}
                  className="flex items-baseline justify-between gap-2 border-b border-slate-50 pb-0.5 dark:border-slate-800/60"
                >
                  <span className="min-w-0 truncate">
                    <span className="font-mono text-[10px] text-slate-400">#{e.id}</span>{" "}
                    <span className="font-medium text-slate-700 dark:text-slate-200">
                      {e.actor}
                    </span>{" "}
                    <span className="text-slate-500 dark:text-slate-400">{e.action}</span>{" "}
                    <span className="font-mono text-[10px] text-slate-500 dark:text-slate-400">
                      {e.resource}
                    </span>
                  </span>
                  <span
                    className="shrink-0 text-[10px] text-slate-400 dark:text-slate-500"
                    title={`${e.purpose_code} · ${e.org_path} · ${new Date(e.occurred_at).toLocaleString()}`}
                  >
                    {timeAgo(e.occurred_at)}
                  </span>
                </li>
              ))}
              {entries.length === 0 && (
                <li className="text-slate-400 dark:text-slate-500">no matching entries</li>
              )}
            </ul>
            {entries.length > 0 && entries.length % PAGE === 0 && (
              <button
                onClick={() => load(offset)}
                disabled={listBusy}
                className="mt-1 text-[10px] text-sky-600 hover:underline focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-sky-400"
              >
                load older entries
              </button>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}
