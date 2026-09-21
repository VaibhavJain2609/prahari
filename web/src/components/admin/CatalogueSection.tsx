"use client";

import { useState } from "react";
import { api, ApiError, SyncResult } from "@/lib/api";
import { useBFF } from "@/lib/use-bff";
import Panel from "@/components/ui/Panel";
import { timeAgo } from "@/lib/format";

// Catalogue sync operations: the manual trigger (audited `sync_trigger`,
// admin) with the registry's own run history underneath — what each pull
// saw/added/updated/absent and where it failed. Camera onboarding itself
// (register form, CSV import) lives on /cameras, not here.
export default function CatalogueSection() {
  const { data: runs, revalidate } = useBFF<SyncResult[]>("sync/runs?limit=8");
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);

  async function onSync() {
    setSyncBusy(true);
    setSyncError(null);
    setSyncMsg(null);
    try {
      const run = await api.triggerSync();
      setSyncMsg(`sync ${run.ok ? "finished" : "failed"} — ${run.cameras_seen} seen`);
      revalidate();
    } catch (err) {
      setSyncError(err instanceof ApiError ? err.message : "sync failed");
    } finally {
      setSyncBusy(false);
    }
  }

  return (
    <Panel title="Catalogue">
      <div className="flex items-center gap-2">
        <button
          onClick={onSync}
          disabled={syncBusy}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          {syncBusy ? "Syncing…" : "Run catalogue sync"}
        </button>
        {syncMsg && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400">{syncMsg}</span>
        )}
      </div>
      {syncError && (
        <p className="mt-1 text-xs text-red-600 dark:text-red-400">{syncError}</p>
      )}

      {runs && runs.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs">
          {runs.map((run, i) => (
            <li key={`${run.started_at}-${i}`} className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 truncate">
                <span
                  className={`font-medium ${
                    run.ok
                      ? "text-emerald-700 dark:text-emerald-400"
                      : "text-red-600 dark:text-red-400"
                  }`}
                >
                  {run.ok ? "ok" : "failed"}
                </span>{" "}
                <span className="text-slate-600 dark:text-slate-300">
                  {run.cameras_seen} seen · +{run.cameras_added} · ~{run.cameras_updated} ·{" "}
                  −{run.cameras_absent}
                </span>
                {run.error && (
                  <span className="text-red-500 dark:text-red-400"> {run.error}</span>
                )}
              </span>
              <span className="shrink-0 text-[10px] text-slate-400 dark:text-slate-500">
                {timeAgo(run.started_at)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-2 text-[10px] text-slate-400 dark:text-slate-500">
        Camera onboarding — register form + CSV import — lives on /cameras.
      </p>
    </Panel>
  );
}
