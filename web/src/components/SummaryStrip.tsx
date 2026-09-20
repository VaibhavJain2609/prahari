"use client";

import { useBFF } from "@/lib/use-bff";
import { CamerasSummary } from "@/lib/api";
import { HEALTH_COLORS, HEALTH_LABELS, HealthState } from "@/lib/health";

// One-line estate summary across the top of the console — the numbers an
// operator glances at before deciding whether the map needs attention.
// 30s refresh: health changes on heartbeat cadence, not per render. A failed
// fetch degrades to a quiet note, not an error banner — the map and rail
// carry their own states.
export default function SummaryStrip() {
  const { data, error, loading } = useBFF<CamerasSummary>("cameras/summary", {
    refreshMs: 30_000,
  });

  return (
    <div className="flex h-10 items-center gap-4 border-b border-slate-200 bg-white px-4 text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
      {loading && !data ? (
        <span className="animate-pulse text-slate-400">loading summary…</span>
      ) : error ? (
        <span className="text-slate-400" title={error.message}>
          summary unavailable
        </span>
      ) : data ? (
        <>
          <Stat label="Active" value={data.active} />
          <span className="flex items-center gap-2" aria-label="health breakdown">
            {(Object.keys(HEALTH_LABELS) as HealthState[]).map((state) => {
              const n = data.health?.[state] ?? 0;
              return (
                <span key={state} className="flex items-center gap-1" title={HEALTH_LABELS[state]}>
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: HEALTH_COLORS[state] }}
                  />
                  {n}
                </span>
              );
            })}
          </span>
          <Stat label="Absent" value={data.absent} />
          <Stat label="Decommissioned" value={data.decommissioned} />
        </>
      ) : null}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <span>
      <span className="font-semibold text-slate-800 dark:text-slate-100">{value}</span>{" "}
      <span className="text-slate-500 dark:text-slate-400">{label}</span>
    </span>
  );
}
