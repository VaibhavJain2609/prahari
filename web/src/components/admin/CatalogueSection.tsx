"use client";

import { useRef, useState } from "react";
import { api, ApiError, ImportResult, SyncResult } from "@/lib/api";
import { useBFF } from "@/lib/use-bff";
import Panel from "@/components/ui/Panel";
import { timeAgo } from "@/lib/format";

// Catalogue operations: the manual sync trigger (audited `sync_trigger`,
// admin) with the registry's own run history underneath, and the CSV
// camera import — the bulk onboarding path whose per-row results are the
// honest answer ("row 14 failed" is actionable, "400 Bad Request" is not).
export default function CatalogueSection() {
  const { data: runs, revalidate } = useBFF<SyncResult[]>("sync/runs?limit=8");
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);

  const fileRef = useRef<HTMLInputElement>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

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

  async function onImport() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setImportBusy(true);
    setImportError(null);
    setImportResult(null);
    try {
      const result = await api.importCameras(await file.text());
      setImportResult(result);
      if (fileRef.current) fileRef.current.value = "";
    } catch (err) {
      setImportError(err instanceof ApiError ? err.message : "import failed");
    } finally {
      setImportBusy(false);
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

      <div className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
        <p className="mb-1.5 text-[11px] text-slate-500 dark:text-slate-400">
          CSV import — one <span className="font-mono">external_id</span> column
          minimum; per-row results below.
        </p>
        <div className="flex items-center gap-1.5">
          <input
            ref={fileRef}
            type="file"
            accept=".csv,text/csv"
            aria-label="Cameras CSV file"
            className="w-44 text-[11px] text-slate-500 file:mr-2 file:rounded file:border file:border-slate-300 file:px-2 file:py-1 file:text-xs file:text-slate-700 dark:file:border-slate-700 dark:file:text-slate-300"
          />
          <button
            onClick={onImport}
            disabled={importBusy}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            {importBusy ? "Importing…" : "Import"}
          </button>
        </div>
        {importError && (
          <p className="mt-1 text-xs text-red-600 dark:text-red-400">{importError}</p>
        )}
        {importResult && (
          <div className="mt-1.5 text-xs">
            <p
              className={
                importResult.failed === 0
                  ? "text-emerald-700 dark:text-emerald-400"
                  : "text-amber-700 dark:text-amber-400"
              }
            >
              {importResult.succeeded}/{importResult.total} imported
              {importResult.failed > 0 && ` · ${importResult.failed} failed`}
            </p>
            {importResult.failed > 0 && (
              <ul className="mt-1 max-h-24 space-y-0.5 overflow-y-auto">
                {importResult.rows
                  .filter((r) => !r.ok)
                  .map((r) => (
                    <li key={r.row} className="text-[10px] text-red-600 dark:text-red-400">
                      row {r.row}: {r.error}
                    </li>
                  ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
