"use client";

import { useState } from "react";
import { api, ApiError, ImportResult, ProbeResult } from "@/lib/api";

// Manual add (probe-before-save) and bulk CSV import — the two onboarding
// paths every operator has, regardless of board. A local body leans on
// these hardest (it registers its own analog estate) but an org or state
// admin can register a camera too, so this isn't gated by org kind, only by
// role (operator+ — see Sidebar.tsx).
export default function OnboardingPanel() {
  return (
    <div className="space-y-4">
      <ManualAdd />
      <CsvImport />
    </div>
  );
}

function ManualAdd() {
  const [externalId, setExternalId] = useState("");
  const [siteName, setSiteName] = useState("");
  const [rtspUrl, setRtspUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [caseRef, setCaseRef] = useState("");
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  async function onProbe() {
    if (!rtspUrl.trim()) return;
    setProbing(true);
    setMessage(null);
    setProbe(null);
    try {
      setProbe(
        await api.probeCamera(
          {
            rtsp_url: rtspUrl.trim(),
            username: username || undefined,
            password: password || undefined,
          },
          caseRef.trim() || undefined,
        ),
      );
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "probe failed" });
    } finally {
      setProbing(false);
    }
  }

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (!externalId.trim() || !siteName.trim()) return;
    setSaving(true);
    setMessage(null);
    try {
      await api.createCamera({
        external_id: externalId.trim(),
        site_name: siteName.trim(),
        camera_type: "analog",
        rtsp_url: rtspUrl.trim() || undefined,
        stream_username: username || undefined,
        stream_password: password || undefined,
      });
      setMessage({ ok: true, text: `${externalId} registered.` });
      setExternalId("");
      setSiteName("");
      setRtspUrl("");
      setUsername("");
      setPassword("");
      setProbe(null);
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "save failed" });
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSave} className="space-y-2">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">Register a camera</p>
      <input
        value={externalId}
        onChange={(e) => setExternalId(e.target.value)}
        placeholder="external id"
        aria-label="External ID"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        value={siteName}
        onChange={(e) => setSiteName(e.target.value)}
        placeholder="site name"
        aria-label="Site name"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        value={rtspUrl}
        onChange={(e) => setRtspUrl(e.target.value)}
        placeholder="rtsp://..."
        aria-label="RTSP URL"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <div className="flex gap-2">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="username (optional)"
          aria-label="Stream username (optional)"
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password (optional)"
          aria-label="Stream password (optional)"
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />
      </div>
      <input
        value={caseRef}
        onChange={(e) => setCaseRef(e.target.value)}
        placeholder="case reference (optional — appended to probe's audit purpose)"
        aria-label="Case reference for probe audit (optional)"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onProbe}
          disabled={probing || !rtspUrl.trim()}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:hover:bg-slate-800 dark:focus:ring-slate-500"
        >
          {probing ? "Probing…" : "Probe"}
        </button>
        <button
          type="submit"
          disabled={saving || !externalId.trim() || !siteName.trim()}
          className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>

      {probe && (
        <div className="rounded border border-slate-200 px-2 py-1 text-[11px] text-slate-600 dark:border-slate-800 dark:text-slate-400">
          {probe.reachable ? "reachable" : "unreachable"}
          {probe.codec ? ` · ${probe.codec}` : ""}
          {probe.declared_fps != null ? ` · ${probe.declared_fps} fps (declared)` : ""}
          {probe.status_message ? ` — ${probe.status_message}` : ""}
        </div>
      )}
      {message && (
        <p className={`text-xs ${message.ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function CsvImport() {
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
      setResult(await api.importCameras(text));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "import failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p className="mb-1 text-xs font-medium text-slate-700 dark:text-slate-300">Bulk import (CSV)</p>
      <label className="block cursor-pointer rounded border border-dashed border-slate-300 px-2 py-2 text-center text-xs text-slate-500 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:hover:bg-slate-800 dark:focus:ring-slate-500">
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
