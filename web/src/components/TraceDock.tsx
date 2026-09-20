"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, RouteResult } from "@/lib/api";
import {
  composePurpose,
  PurposeAction,
  PURPOSE_ACTIONS,
  PurposePrompt,
  usePurpose,
} from "@/lib/purpose";
import { LinkKindBadge } from "@/components/ui/Badge";
import { FlyTo } from "@/components/CameraMap";

// The mandatory path's UI (was PlateTracePanel): a registration number in,
// a timestamped, location-wise route out — now docked over the map so the
// hop list and the drawn route are read together. The route lookup is
// audited: the operator's purpose (category + case ref) rides in
// X-Purpose-Code so the hash-chained log records why the trace was run.
export default function TraceDock({
  route,
  onRoute,
  tracePlate,
  onConsumeTrace,
  onFlyTo,
  onCameraSelect,
}: {
  route: RouteResult | null;
  onRoute: (route: RouteResult | null) => void;
  // `?trace=<plate>` deep link: prefill + auto-run once a purpose exists.
  tracePlate: string | null;
  onConsumeTrace: () => void;
  onFlyTo?: (to: Omit<FlyTo, "seq">) => void;
  onCameraSelect?: (cameraId: string) => void;
}) {
  const { purpose, setPurpose } = usePurpose();
  const [collapsed, setCollapsed] = useState(false);
  const [plate, setPlate] = useState("");
  // Draft purpose fields, seeded from the session purpose — writing them
  // through sets the shared context so the camera drawer reuses it.
  const [action, setAction] = useState<PurposeAction>(purpose?.action ?? "plate-trace");
  const [ref, setRef] = useState(purpose?.ref ?? "");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);
  const [rejectedOpen, setRejectedOpen] = useState(false);
  const autoRan = useRef<string | null>(null);

  async function runTrace(plateValue: string, purposeCode: string) {
    setLoading(true);
    setError(null);
    try {
      onRoute(await api.getRoute(plateValue, purposeCode));
    } catch (err) {
      onRoute(null);
      setError(err instanceof ApiError ? err.message : "route lookup failed");
    } finally {
      setLoading(false);
    }
  }

  // `?trace=` handling: auto-run the moment a purpose exists — either
  // already set this session, or as soon as the operator completes the
  // required fields / the inline prompt below. Once per param value; the
  // setState calls live inside the promise callbacks, not the effect body.
  useEffect(() => {
    if (!tracePlate || !purpose || autoRan.current === tracePlate) return;
    const code = composePurpose(purpose);
    if (!code) return;
    autoRan.current = tracePlate;
    let cancelled = false;
    api
      .getRoute(tracePlate, code)
      .then((r) => {
        if (cancelled) return;
        onRoute(r);
        setPlate(tracePlate);
        setCollapsed(false);
        onConsumeTrace();
      })
      .catch((err) => {
        if (cancelled) return;
        onRoute(null);
        setPlate(tracePlate);
        setCollapsed(false);
        setError(err instanceof ApiError ? err.message : "route lookup failed");
      });
    return () => {
      cancelled = true;
    };
  }, [tracePlate, purpose, onRoute, onConsumeTrace]);

  // A consumed param clears autoRan so a later `?trace=` for the same plate
  // still runs.
  useEffect(() => {
    if (!tracePlate) autoRan.current = null;
  }, [tracePlate]);

  const promptNeeded = Boolean(tracePlate && !purpose);

  function onTrace(e: React.FormEvent) {
    e.preventDefault();
    const p = { action, ref: ref.trim() };
    const code = composePurpose(p);
    if (!plate.trim() || !code) return;
    setPurpose(p);
    // A manual submit satisfies a pending ?trace= deep link — otherwise the
    // freshly-set purpose would trigger the auto-run on top of this one.
    if (tracePlate) {
      autoRan.current = tracePlate;
      onConsumeTrace();
    }
    void runTrace(plate.trim(), code);
  }

  async function onExport(format: "csv" | "pdf") {
    if (!route) return;
    const code = composePurpose(purpose ?? { action, ref: ref.trim() });
    if (!code) return;
    setExporting(format);
    try {
      const blob = await api.exportRoute(route.plate, format, code);
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

  // Even collapsed, a pending `?trace=` without a purpose expands — the
  // prompt is the whole point of the deep link.
  if (collapsed && !promptNeeded) {
    return (
      <button
        onClick={() => setCollapsed(false)}
        className="absolute left-4 top-4 z-10 rounded-lg bg-white/95 px-3 py-2 text-xs font-medium text-slate-700 shadow-md hover:bg-white focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-900/95 dark:text-slate-200 dark:hover:bg-slate-900"
      >
        Plate trace {route ? `· ${route.plate}` : ""}
      </button>
    );
  }

  return (
    <div className="absolute left-4 top-4 z-10 flex max-h-[calc(100%-2rem)] w-96 flex-col rounded-lg bg-white/95 p-3 shadow-md dark:bg-slate-900/95">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Plate trace</h2>
        <button
          onClick={() => setCollapsed(true)}
          aria-label="Collapse trace panel"
          className="rounded px-1.5 py-0.5 text-xs text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800"
        >
          –
        </button>
      </div>

      {promptNeeded && (
        <div className="mb-2">
          <p className="mb-1 text-[11px] text-slate-500 dark:text-slate-400">
            Trace requested for <span className="font-mono">{tracePlate}</span> — set a purpose
            to run it.
          </p>
          <PurposePrompt />
        </div>
      )}

      <form onSubmit={onTrace} className="mb-2 space-y-1.5">
        <div className="flex gap-2">
          <label htmlFor="trace-plate" className="sr-only">
            Registration number
          </label>
          <input
            id="trace-plate"
            value={plate}
            onChange={(e) => setPlate(e.target.value.toUpperCase())}
            placeholder="GJ01AB1234"
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs uppercase focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
          />
          <button
            type="submit"
            disabled={loading || !plate.trim()}
            className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
          >
            {loading ? "…" : "Trace"}
          </button>
        </div>
        <div className="flex gap-2">
          <label htmlFor="trace-purpose" className="sr-only">
            Purpose
          </label>
          <select
            id="trace-purpose"
            required
            value={action}
            onChange={(e) => setAction(e.target.value as PurposeAction)}
            className="rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
          >
            {PURPOSE_ACTIONS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
          <label htmlFor="trace-case-ref" className="sr-only">
            Case reference
          </label>
          <input
            id="trace-case-ref"
            required
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            placeholder="case ref (audited)"
            className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
          />
        </div>
      </form>

      {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}

      {route && (
        <div className="min-h-0 overflow-y-auto">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {route.hops.length} hop{route.hops.length === 1 ? "" : "s"}
              {route.rejected.length > 0 &&
                ` · ${route.rejected.length} rejected (infeasible)`}
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

          <ol className="space-y-1 text-xs">
            {route.hops.map((hop, i) => (
              <li
                key={`${hop.camera_id}-${i}`}
                className="rounded border border-slate-200 px-2 py-1 dark:border-slate-800"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-slate-800 dark:text-slate-100">
                    <span className="mr-1 text-slate-400">{i + 1}.</span>
                    {hop.camera_id}
                  </span>
                  <span className="flex items-center gap-1">
                    <LinkKindBadge kind={hop.link_kind} />
                    {hop.location && onFlyTo && (
                      <button
                        onClick={() =>
                          onFlyTo({
                            latitude: hop.location!.latitude,
                            longitude: hop.location!.longitude,
                          })
                        }
                        aria-label={`Fly to ${hop.camera_id}`}
                        title="Show on map"
                        className="rounded px-1 text-[10px] text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800"
                      >
                        map
                      </button>
                    )}
                    {onCameraSelect && (
                      <button
                        onClick={() => onCameraSelect(hop.camera_id)}
                        aria-label={`Open camera ${hop.camera_id}`}
                        title="Camera detail"
                        className="rounded px-1 text-[10px] text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800"
                      >
                        ›
                      </button>
                    )}
                  </span>
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

          {route.rejected.length > 0 && (
            <div className="mt-2">
              <button
                onClick={() => setRejectedOpen((o) => !o)}
                aria-expanded={rejectedOpen}
                className="text-[11px] text-slate-500 underline decoration-dotted hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:text-slate-300"
              >
                {rejectedOpen ? "Hide" : "Show"} {route.rejected.length} rejected sighting
                {route.rejected.length === 1 ? "" : "s"}
              </button>
              {rejectedOpen && (
                <ul className="mt-1 space-y-1 text-[11px]">
                  {route.rejected.map((r, i) => (
                    <li
                      key={i}
                      className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-slate-600 dark:border-amber-900 dark:bg-amber-950/40 dark:text-slate-400"
                    >
                      {r.from_camera_id} → {r.to_camera_id}: {r.reason}
                      {r.implied_speed_kmh != null &&
                        ` (${r.implied_speed_kmh.toFixed(0)} km/h implied)`}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {route.dark_zones.length > 0 && (
            <p className="mt-2 rounded border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
              Route crosses {route.dark_zones.length} dark zone
              {route.dark_zones.length === 1 ? "" : "s"} — coverage gaps are ringed on the map.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
