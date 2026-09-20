"use client";

import { useState } from "react";
import { api, ApiError, ImportResult } from "@/lib/api";

// Bulk CSV import (was OnboardingPanel's CsvImport). Needs an external_id
// column; per-row failures come back in the response rather than failing
// the whole file.
export default function CsvImport({ onImported }: { onImported?: () => void }) {
  const [fileName, setFileName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setFileName(file.name);
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const text = await file.text();
      const res = await api.importCameras(text);
      setResult(res);
      if (res.succeeded > 0) onImported?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "import failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <label className="block cursor-pointer rounded border border-dashed border-slate-300 px-2 py-2 text-center text-xs text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:focus:ring-slate-500">
        {busy ? "Importing…" : fileName ?? "Choose a CSV file (needs an external_id column)"}
        <input
          type="file"
          accept=".csv,text/csv"
          className="hidden"
          onChange={onFile}
          disabled={busy}
          aria-label="Choose a CSV file to import"
        />
      </label>
      {error && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{error}</p>}
      {result && (
        <div className="mt-1 text-xs text-slate-600 dark:text-slate-400">
          <p>
            {result.succeeded}/{result.total} rows imported
            {result.failed > 0 ? `, ${result.failed} failed` : ""}.
          </p>
          {result.failed > 0 && (
            <ul className="mt-1 max-h-32 space-y-0.5 overflow-y-auto">
              {result.rows
                .filter((r) => !r.ok)
                .map((r) => (
                  <li key={r.row} className="text-red-600 dark:text-red-400">
                    row {r.row} ({r.external_id ?? "?"}): {r.error}
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
