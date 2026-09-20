"use client";

import { useCallback, useState } from "react";
import { useAlertStream, useSSEStatus } from "@/lib/alerts";

// The alert payload is the Alert proto serialized by the BFF with
// preserving_proto_field_name — snake_case keys, enums as their proto names
// ("ALERT_PRIORITY_CRITICAL"), timestamps as RFC3339 strings. Read
// defensively: a schema addition must never break the rail.
type AlertItem = {
  id: string;
  raw: Record<string, unknown>;
};

const MAX_ALERTS = 20;

const PRIORITY_STYLES: Record<string, string> = {
  ALERT_PRIORITY_CRITICAL:
    "bg-red-600 text-white",
  ALERT_PRIORITY_HIGH:
    "bg-orange-500 text-white",
  ALERT_PRIORITY_MEDIUM:
    "bg-amber-400 text-amber-950",
  ALERT_PRIORITY_LOW:
    "bg-slate-300 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
};

const REASON_LABELS: Record<string, string> = {
  WATCHLIST_REASON_STOLEN: "stolen",
  WATCHLIST_REASON_WANTED: "wanted",
  WATCHLIST_REASON_MISSING_PERSON: "missing person",
  WATCHLIST_REASON_BLACKLISTED: "blacklisted",
  WATCHLIST_REASON_SUSPECT: "suspect",
};

// Live relay off the BFF's SSE endpoint via the shared stream in
// lib/alerts.ts — the same EventSource drives the header's status dot, so
// this panel never opens a second upstream subscription.
export default function AlertPanel() {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const state = useSSEStatus();

  const onAlert = useCallback((raw: Record<string, unknown>) => {
    setAlerts((prev) => {
      // Dedup on alert_id: the match engine already dedupes on
      // (camera, plate, time-bucket), but a reconnected stream or a retry
      // can replay the same alert — keying on alert_id absorbs that instead
      // of minting a fresh row per delivery.
      const id = (raw.alert_id as string) ?? null;
      if (id && prev.some((a) => a.id === id)) return prev;
      return [{ id: id ?? `no-id-${prev.length}-${Date.now()}`, raw }, ...prev].slice(
        0,
        MAX_ALERTS,
      );
    });
  }, []);
  useAlertStream(onAlert);

  function toggleExpanded(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
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
        {state === "unavailable" && "alert stream not configured for this deployment"}
        {state === "error" && "reconnecting…"}
      </div>

      {alerts.length === 0 ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">No alerts yet this session.</p>
      ) : (
        <ul aria-live="polite" className="max-h-56 space-y-1 overflow-y-auto text-xs">
          {alerts.map((a) => {
            const matched = (a.raw.matched_entry ?? {}) as Record<string, unknown>;
            const explanation = a.raw.explanation as Record<string, unknown> | undefined;
            const priority = a.raw.priority as string | undefined;
            const reason = matched.reason as string | undefined;
            const raisedAt = a.raw.raised_at as string | undefined;
            const isOpen = expanded.has(a.id);
            return (
              <li
                key={a.id}
                className="rounded border border-amber-200 bg-amber-50 px-2 py-1 dark:border-amber-900 dark:bg-amber-950/40"
              >
                <div className="flex items-center gap-1.5">
                  {priority && (
                    <span
                      className={`rounded px-1 py-px text-[10px] font-semibold ${
                        PRIORITY_STYLES[priority] ??
                        "bg-slate-300 text-slate-800 dark:bg-slate-700 dark:text-slate-100"
                      }`}
                    >
                      {priority.replace("ALERT_PRIORITY_", "")}
                    </span>
                  )}
                  <span className="font-medium text-slate-800 dark:text-slate-100">
                    {summarize(a.raw)}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2 text-slate-500 dark:text-slate-400">
                  <span>
                    {reason && (REASON_LABELS[reason] ?? reason.replace("WATCHLIST_REASON_", "").toLowerCase())}
                    {reason && raisedAt && " · "}
                    {raisedAt ? timeAgo(raisedAt) : ""}
                  </span>
                  {explanation && (
                    <button
                      onClick={() => toggleExpanded(a.id)}
                      aria-expanded={isOpen}
                      aria-label={`${isOpen ? "Hide" : "Show"} match explanation for ${a.id}`}
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

function summarize(raw: Record<string, unknown>): string {
  const detection = (raw.detection ?? {}) as Record<string, unknown>;
  const plate = detection.plate as Record<string, unknown> | undefined;
  const plateText =
    (plate?.normalised_text as string) ??
    (plate?.raw_text as string) ??
    (raw.plate as string);
  const camera = (detection.camera_id as string) ?? (raw.camera_id as string);
  if (plateText && camera) return `${plateText} at ${camera}`;
  if (plateText) return plateText;
  if (camera) return `activity at ${camera}`;
  return "alert";
}

function formatNumber(value: unknown): string {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : "?";
}

// Alerts are judged by when they were raised upstream, not when this tab
// happened to receive them — a reconnecting stream would otherwise redate
// every alert it replays.
function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
