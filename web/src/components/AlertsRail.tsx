"use client";

import { useCallback, useState } from "react";
import { useAlertStream, useAlertStreamState } from "@/components/AlertStreamProvider";
import { PriorityBadge } from "@/components/ui/Badge";
import { enumLabel, timeAgo } from "@/lib/format";

// The alert payload is the Alert proto serialized by the BFF with
// preserving_proto_field_name — snake_case keys, enums as their proto names
// ("ALERT_PRIORITY_CRITICAL"), timestamps as RFC3339 strings. Read
// defensively: a schema addition must never break the rail.
type AlertItem = {
  // Rows key on dedup_key — the match engine's (camera, plate, time-bucket)
  // identity — falling back to alert_id, then a synthetic key.
  key: string;
  count: number;
  raw: Record<string, unknown>;
};

const MAX_ALERTS = 50;

const REASON_LABELS: Record<string, string> = {
  WATCHLIST_REASON_STOLEN: "stolen",
  WATCHLIST_REASON_WANTED: "wanted",
  WATCHLIST_REASON_MISSING_PERSON: "missing person",
  WATCHLIST_REASON_BLACKLISTED: "blacklisted",
  WATCHLIST_REASON_SUSPECT: "suspect",
};

// Live relay off the BFF's SSE endpoint via the shared stream (one
// EventSource for the whole console — see AlertStreamProvider). Clicking an
// alert opens that camera's drawer rather than expanding detail here.
export default function AlertsRail({
  onCameraSelect,
}: {
  onCameraSelect?: (cameraId: string) => void;
}) {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const state = useAlertStreamState();

  const onAlert = useCallback((raw: Record<string, unknown>) => {
    setAlerts((prev) => {
      const key =
        (raw.dedup_key as string) ??
        (raw.alert_id as string) ??
        `no-key-${prev.length}-${Date.now()}`;
      const existing = prev.find((a) => a.key === key);
      if (existing) {
        // Same (camera, plate, bucket) again — fold into the row with a ×n
        // counter instead of a second row. Newest raw wins so the expanded
        // explanation reflects the latest match, and the row floats back to
        // the top since it just happened again.
        const merged = { ...existing, count: existing.count + 1, raw };
        return [merged, ...prev.filter((a) => a.key !== key)].slice(0, MAX_ALERTS);
      }
      return [{ key, count: 1, raw }, ...prev].slice(0, MAX_ALERTS);
    });
  }, []);
  useAlertStream(onAlert);

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Alerts</h2>
        <span className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              state === "open"
                ? "bg-emerald-500"
                : state === "connecting" || state === "error"
                  ? "bg-amber-500"
                  : "bg-slate-400"
            }`}
          />
          {state === "open" && "live"}
          {state === "connecting" && "connecting…"}
          {state === "unavailable" && "stream not configured"}
          {state === "error" && "reconnecting…"}
        </span>
      </div>

      {alerts.length === 0 ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">No alerts yet this session.</p>
      ) : (
        <ul aria-live="polite" className="min-h-0 flex-1 space-y-1 overflow-y-auto text-xs">
          {alerts.map((a) => {
            const matched = (a.raw.matched_entry ?? {}) as Record<string, unknown>;
            const explanation = a.raw.explanation as Record<string, unknown> | undefined;
            const priority = a.raw.priority as string | undefined;
            const reason = matched.reason as string | undefined;
            const raisedAt = a.raw.raised_at as string | undefined;
            const cameraId = cameraIdOf(a.raw);
            const isOpen = expanded.has(a.key);
            return (
              <li
                key={a.key}
                className="rounded border border-amber-200 bg-amber-50 px-2 py-1 dark:border-amber-900 dark:bg-amber-950/40"
              >
                <div className="flex items-center gap-1.5">
                  {priority && <PriorityBadge priority={priority} />}
                  <button
                    onClick={() => cameraId && onCameraSelect?.(cameraId)}
                    disabled={!cameraId || !onCameraSelect}
                    title={cameraId ? `Open ${cameraId}` : undefined}
                    className="min-w-0 flex-1 truncate text-left font-medium text-slate-800 enabled:hover:underline focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-100"
                  >
                    {summarize(a.raw)}
                  </button>
                  {a.count > 1 && (
                    <span
                      className="rounded bg-slate-200 px-1 text-[10px] font-semibold text-slate-700 dark:bg-slate-700 dark:text-slate-200"
                      title={`${a.count} alerts in this dedup bucket`}
                    >
                      ×{a.count}
                    </span>
                  )}
                </div>
                <div className="flex items-center justify-between gap-2 text-slate-500 dark:text-slate-400">
                  <span>
                    {reason && (REASON_LABELS[reason] ?? enumLabel(reason, "WATCHLIST_REASON_"))}
                    {reason && raisedAt && " · "}
                    {raisedAt ? timeAgo(raisedAt) : ""}
                  </span>
                  {explanation && (
                    <button
                      onClick={() => toggleExpanded(a.key)}
                      aria-expanded={isOpen}
                      aria-label={`${isOpen ? "Hide" : "Show"} match explanation for ${a.key}`}
                      className="rounded px-1 text-[10px] text-slate-500 underline decoration-dotted hover:text-slate-700 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:hover:text-slate-300"
                    >
                      {isOpen ? "less" : "why?"}
                    </button>
                  )}
                </div>
                {isOpen && explanation && (
                  <div className="mt-1 border-t border-amber-200 pt-1 text-[11px] text-slate-600 dark:border-amber-900 dark:text-slate-400">
                    <div>
                      observed <span className="font-mono">{String(explanation.observed_plate ?? "?")}</span>
                      {" → "}
                      matched <span className="font-mono">{String(explanation.matched_plate ?? "?")}</span>
                    </div>
                    <div>
                      score {formatNumber(explanation.final_score)} ·{" "}
                      {Array.isArray(explanation.edits) ? explanation.edits.length : 0} edit
                      {Array.isArray(explanation.edits) && explanation.edits.length === 1 ? "" : "s"}
                      {explanation.format_plausibility != null &&
                        ` · plausibility ${formatNumber(explanation.format_plausibility)}`}
                    </div>
                    {(matched.case_reference as string) && (
                      <div>case {String(matched.case_reference)}</div>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function cameraIdOf(raw: Record<string, unknown>): string | null {
  const detection = (raw.detection ?? {}) as Record<string, unknown>;
  return ((detection.camera_id as string) ?? (raw.camera_id as string)) || null;
}

function summarize(raw: Record<string, unknown>): string {
  const detection = (raw.detection ?? {}) as Record<string, unknown>;
  const plate = detection.plate as Record<string, unknown> | undefined;
  const plateText =
    (plate?.normalised_text as string) ??
    (plate?.raw_text as string) ??
    (raw.plate as string);
  const camera = cameraIdOf(raw);
  if (plateText && camera) return `${plateText} at ${camera}`;
  if (plateText) return plateText;
  if (camera) return `activity at ${camera}`;
  return "alert";
}

function formatNumber(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "?";
}
