"use client";

import { useState } from "react";
import { api, ApiError, RouteResult } from "@/lib/api";

// The mandatory path's UI: a registration number in, a timestamped,
// location-wise route out. Never org-scoped — see the BFF's own comment on
// GET /api/v1/routes/{plate} — so this is the one panel every board shows
// identically. The route lookup is audited: the case reference rides in
// X-Purpose-Code so the hash-chained log records why the trace was run.
export default function PlateTracePanel() {
  const [plate, setPlate] = useState("");
  const [caseRef, setCaseRef] = useState("");
  const [route, setRoute] = useState<RouteResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);

  async function onTrace(e: React.FormEvent) {
    e.preventDefault();
    if (!plate.trim() || !caseRef.trim()) return;
    setLoading(true);
    setError(null);
    setRoute(null);
    try {
      setRoute(await api.getRoute(plate.trim(), caseRef.trim()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "route lookup failed");
    } finally {
      setLoading(false);
    }
  }

  async function onExport(format: "csv" | "pdf") {
    if (!route) return;
    setExporting(format);
    try {
      const blob = await api.exportRoute(route.plate, format, caseRef.trim() || undefined);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `route-${route.plate}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "export failed");
    } finally {
      setExporting(null);
    }
  }

  return (
    <div>
      <form onSubmit={onTrace} className="mb-2 space-y-1.5">
        <div className="flex gap-2">
          <label htmlFor="plate-trace-plate" className="sr-only">
            Registration number
          </label>
          <input
            id="plate-trace-plate"
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase())}
            placeholder="GJ01AB1234"
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs uppercase focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
          />
          <button
            type="submit"
            disabled={loading || !plate.trim() || !caseRef.trim()}
            className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
          >
            {loading ? "…" : "Trace"}
          </button>
        </div>
        <label htmlFor="plate-trace-case-ref" className="sr-only">
          Case reference / purpose
        </label>
        <input
          id="plate-trace-case-ref"
          required
          value={caseRef}
          onChange={(e) => setCaseRef(e.target.value)}
          placeholder="Case reference / purpose (required, audited)"
          className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />
      </form>

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

      {route && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {route.hops.length} hop{route.hops.length === 1 ? "" : "s"}
              {route.rejected.length > 0 &&
                ` · ${route.rejected.length} rejected (infeasible)`}
              {route.dark_zones.length > 0 &&
                ` · ${route.dark_zones.length} dark zone${route.dark_zones.length === 1 ? "" : "s"}`}
            </span>
            <div className="flex gap-1">
              <button
                onClick={() => onExport("csv")}
                disabled={exporting !== null}
                aria-label="Export route as CSV"
                className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
              >
                CSV
              </button>
              <button
                onClick={() => onExport("pdf")}
                disabled={exporting !== null}
                aria-label="Export route as PDF"
                className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
              >
                PDF
              </button>
            </div>
          </div>
          <ol className="max-h-48 space-y-1 overflow-y-auto text-xs">
            {route.hops.map((hop, i) => (
              <li
                key={`${hop.camera_id}-${i}`}
                className="rounded border border-slate-200 px-2 py-1 dark:border-slate-800"
              >
                <div className="font-medium text-slate-800 dark:text-slate-100">
                  {hop.camera_id}
                  {hop.link_kind === "bridged" && (
                    <span className="ml-1 font-normal text-slate-400">(bridged)</span>
                  )}
                </div>
                <div className="text-slate-500 dark:text-slate-400">
                  {hop.wall_clock_s != null
                    ? new Date(hop.wall_clock_s * 1000).toLocaleString()
                    : "no timestamp"}
                  {hop.confidence != null ? ` · ${(hop.confidence * 100).toFixed(0)}%` : ""}
                </div>
              </li>
            ))}
          </ol>
          {route.hops.length === 0 && (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              No sightings for this plate in scope.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
