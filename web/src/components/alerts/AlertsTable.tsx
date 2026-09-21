"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, ApiError, StoredAlert } from "@/lib/api";
import { useAlertStream, useAlertStreamState } from "@/components/AlertStreamProvider";
import { PriorityBadge } from "@/components/ui/Badge";
import { enumLabel, timeAgo } from "@/lib/format";
import {
  ALERT_LIST_LIMIT,
  AlertFilters,
  alertCameraId,
  alertMatchesFilters,
  alertQueryParams,
  alertRowKey,
  matchedPlate,
  mergeAlertRows,
  observedPlate,
  occurredAtOf,
  rawPlateText,
} from "@/lib/alert-history";

// How long a live-arrived row keeps its "new" tint. Long enough to notice
// while scanning, short enough that a row left on screen isn't still
// claiming to be fresh minutes later.
const FRESH_MS = 15_000;

const REASON_LABELS: Record<string, string> = {
  WATCHLIST_REASON_STOLEN: "stolen",
  WATCHLIST_REASON_WANTED: "wanted",
  WATCHLIST_REASON_MISSING_PERSON: "missing person",
  WATCHLIST_REASON_BLACKLISTED: "blacklisted",
  WATCHLIST_REASON_SUSPECT: "suspect",
};

// The persisted alert work surface: the match engine's AlertStore (Postgres
// when configured — history survives restarts), filtered server-side where
// the store understands the predicate, plus the shared SSE stream merged in
// so an alert raised this second appears without waiting for a refresh.
// Filters live in the URL (the parent mirrors them) like the cameras page,
// so a filtered view is a shareable link.
export default function AlertsTable({
  filters,
  onFilters,
}: {
  filters: AlertFilters;
  onFilters: (next: Partial<AlertFilters>) => void;
}) {
  const stream = useAlertStreamState();
  const [rows, setRows] = useState<StoredAlert[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [plateDraft, setPlateDraft] = useState(filters.plate);
  const seq = useRef(0);

  // Live rows keyed on their dedupe identity, and which of them are still
  // inside the "new" window. Rows acked in this session are overlaid on the
  // fetched copy so the row flips without waiting for the refetch.
  const [live, setLive] = useState<Map<string, StoredAlert>>(new Map());
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [ackedOverlay, setAckedOverlay] = useState<Map<string, StoredAlert>>(new Map());

  // The SSE callback is registered once and reads the current filters
  // through a ref — resubscribing on every keystroke would churn the
  // listener set for nothing. The ref is written in an effect, not during
  // render (react-hooks/refs).
  const filtersRef = useRef(filters);
  useEffect(() => {
    filtersRef.current = filters;
  });

  const { plate, camera, since, ack } = filters;
  const [reloadTick, setReloadTick] = useState(0);

  // Debounce the free-text plate filter into the URL — a param write per
  // keystroke is a re-filter per keystroke.
  useEffect(() => {
    if (plateDraft === plate) return;
    const t = setTimeout(() => onFilters({ plate: plateDraft }), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plateDraft]);

  // Refetches on the server-side filters (and an explicit reload tick)
  // only — plate is a client-side substring over the fetched window, so
  // typing in it must not re-query. Latest-fetch-wins plus AbortController,
  // same convention as CamerasTable.
  useEffect(() => {
    const controller = new AbortController();
    const mySeq = ++seq.current;
    async function load() {
      setLoading(true);
      try {
        const result = await api.listAlerts(
          alertQueryParams({ plate: "", camera, since, ack }),
          { signal: controller.signal },
        );
        if (mySeq !== seq.current) return;
        setRows(result);
        setError(null);
      } catch (err) {
        if (mySeq !== seq.current || controller.signal.aborted) return;
        setRows(null);
        setError(err instanceof ApiError ? err.message : "list failed");
      } finally {
        if (mySeq === seq.current) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [camera, since, ack, reloadTick]);

  const onAlert = useCallback((raw: Record<string, unknown>) => {
    const row = raw as StoredAlert;
    if (!alertMatchesFilters(row, filtersRef.current)) return;
    // Same alert_id twice — a redelivery or a frame that survived dedup —
    // replaces the row in place rather than doubling it. Rows without a key
    // still land, each under its own synthetic identity.
    const key = alertRowKey(row, `sse-${Date.now()}-${Math.random()}`);
    setLive((prev) => new Map(prev).set(key, row));
    setFresh((prev) => new Set(prev).add(key));
    setTimeout(
      () =>
        setFresh((prev) => {
          if (!prev.has(key)) return prev;
          const next = new Set(prev);
          next.delete(key);
          return next;
        }),
      FRESH_MS,
    );
  }, []);
  useAlertStream(onAlert);

  // Merge persisted + live, deduped on alert_id (persisted wins — it has
  // the ack columns), newest first, then re-apply the whole filter set:
  // plate substring is client-side by design (upstream is exact-match) and
  // a freshly-acked row under the "unacked" view should drop immediately.
  const persisted = (rows ?? []).map((r, i) => {
    const key = alertRowKey(r, `persisted-${i}`);
    return ackedOverlay.get(key) ?? r;
  });
  const merged = mergeAlertRows(persisted, [...live.values()]);
  const visible = merged.rows.filter((r) => alertMatchesFilters(r, filters));

  const input =
    "rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  function onAcked(updated: StoredAlert) {
    const key = alertRowKey(updated, `ack-${Date.now()}`);
    setAckedOverlay((prev) => new Map(prev).set(key, updated));
    // A live-only row keeps its slot, now with the ack fields — the store
    // insert is async, so deleting it here could make the row vanish until
    // (or unless) the next fetch returns it.
    setLive((prev) => (prev.has(key) ? new Map(prev).set(key, updated) : prev));
    setFresh((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
    setReloadTick((t) => t + 1);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 px-4 py-2">
        <input
          value={plateDraft}
          onChange={(e) => setPlateDraft(e.target.value)}
          placeholder="plate contains…"
          aria-label="Filter by plate"
          className={`${input} w-44 font-mono`}
        />
        <input
          value={camera}
          onChange={(e) => onFilters({ camera: e.target.value })}
          placeholder="camera id"
          aria-label="Filter by camera"
          className={`${input} w-36 font-mono`}
        />
        <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          since
          <input
            type="datetime-local"
            value={since}
            onChange={(e) => onFilters({ since: e.target.value })}
            aria-label="Alerts since"
            className={input}
          />
        </label>
        <select
          value={ack}
          onChange={(e) => onFilters({ ack: e.target.value as AlertFilters["ack"] })}
          aria-label="Filter by acknowledgement"
          className={input}
        >
          <option value="all">all alerts</option>
          <option value="unacked">unacknowledged</option>
          <option value="acked">acknowledged</option>
        </select>
        <button
          onClick={() => setReloadTick((t) => t + 1)}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Refresh
        </button>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          <span
            className={`inline-block h-1.5 w-1.5 rounded-full ${
              stream === "open"
                ? "bg-emerald-500"
                : stream === "connecting" || stream === "error"
                  ? "bg-amber-500"
                  : "bg-slate-400"
            }`}
          />
          {stream === "open" ? "live" : stream === "connecting" ? "connecting…" : stream === "error" ? "reconnecting…" : "stream off"}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
        {error ? (
          <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
        ) : (
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-white text-[11px] uppercase tracking-wide text-slate-400 dark:bg-slate-950">
              <tr>
                <th className="px-2 py-1 font-medium">Occurred</th>
                <th className="px-2 py-1 font-medium">Plate</th>
                <th className="px-2 py-1 font-medium">Match</th>
                <th className="px-2 py-1 font-medium">Camera</th>
                <th className="px-2 py-1 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((row, i) => {
                const key = alertRowKey(row, `row-${i}`);
                return (
                  <AlertRow
                    key={key}
                    row={row}
                    isNew={merged.liveOnly.has(key) && fresh.has(key)}
                    onAcked={onAcked}
                  />
                );
              })}
              {rows && visible.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-2 py-6 text-center text-slate-400">
                    No alerts match these filters.
                  </td>
                </tr>
              )}
              {!rows && loading && (
                <tr>
                  <td colSpan={5} className="px-2 py-6 text-center text-slate-400">
                    <span className="animate-pulse">loading…</span>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
        <span>
          {rows ? `${visible.length} shown` : "…"}
          {rows && rows.length >= ALERT_LIST_LIMIT && ` · latest ${ALERT_LIST_LIMIT}`}
          {loading && " · refreshing"}
        </span>
        <span>
          Acknowledgement is the only lifecycle — no assignment workflow.
        </span>
      </div>
    </div>
  );
}

function AlertRow({
  row,
  isNew,
  onAcked,
}: {
  row: StoredAlert;
  isNew: boolean;
  onAcked: (updated: StoredAlert) => void;
}) {
  const occurred = occurredAtOf(row);
  const cameraId = alertCameraId(row);
  const observed = observedPlate(row);
  const raw = rawPlateText(row);
  const matched = matchedPlate(row);
  const reason = row.matched_entry?.reason;
  const explanation = row.explanation;
  const edits = Array.isArray(explanation?.edits) ? explanation.edits.length : 0;

  return (
    <tr
      className={`border-t border-slate-100 dark:border-slate-800 ${
        isNew
          ? "bg-amber-50 dark:bg-amber-950/30"
          : "hover:bg-slate-50 dark:hover:bg-slate-900"
      }`}
    >
      <td
        className="px-2 py-1 whitespace-nowrap text-slate-600 dark:text-slate-400"
        title={occurred ? new Date(occurred).toLocaleString() : undefined}
      >
        {occurred ? timeAgo(occurred) : "—"}
        {isNew && (
          <span className="ml-1 rounded bg-amber-200 px-1 text-[9px] font-semibold uppercase text-amber-900 dark:bg-amber-900 dark:text-amber-200">
            new
          </span>
        )}
      </td>
      <td className="px-2 py-1">
        <span className="font-mono text-[11px] font-medium text-slate-800 dark:text-slate-200">
          {observed ?? "—"}
        </span>
        {raw && observed && raw !== observed && (
          <div className="font-mono text-[10px] text-slate-400">raw {raw}</div>
        )}
      </td>
      <td className="px-2 py-1">
        <div className="flex items-center gap-1.5">
          {row.priority && <PriorityBadge priority={row.priority} />}
          <span className="font-mono text-[11px] text-slate-700 dark:text-slate-300">
            {matched ?? "—"}
          </span>
          {reason && (
            <span className="text-slate-500 dark:text-slate-400">
              {REASON_LABELS[reason] ?? enumLabel(reason, "WATCHLIST_REASON_")}
            </span>
          )}
        </div>
        {explanation && (
          <div className="text-[10px] text-slate-400 dark:text-slate-500">
            score {formatScore(explanation.final_score)} · {edits} edit
            {edits === 1 ? "" : "s"}
            {row.matched_entry?.case_reference &&
              ` · case ${row.matched_entry.case_reference}`}
          </div>
        )}
      </td>
      <td className="px-2 py-1">
        {cameraId ? (
          // /?camera=<id> — the console page resolves the param into the
          // map's camera drawer.
          <Link
            href={`/?camera=${encodeURIComponent(cameraId)}`}
            className="font-mono text-[11px] text-sky-700 hover:underline focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-sky-300"
          >
            {cameraId}
          </Link>
        ) : (
          <span className="text-slate-400">—</span>
        )}
      </td>
      <td className="px-2 py-1 whitespace-nowrap">
        <AckCell row={row} onAcked={onAcked} />
      </td>
    </tr>
  );
}

// The one lifecycle control. State machine: idle → pending (disabled,
// "acking…") → success (parent swaps in the returned row, so the cell
// re-renders as acked) or error (inline message, button back to retryable).
// No purpose code: the BFF org-scopes the alert's camera and writes the
// `alert_ack` audit entry itself under the internal `admin` purpose.
function AckCell({
  row,
  onAcked,
}: {
  row: StoredAlert;
  onAcked: (updated: StoredAlert) => void;
}) {
  const [state, setState] = useState<
    { status: "idle" } | { status: "pending" } | { status: "error"; message: string }
  >({ status: "idle" });

  if (row.acknowledged_at) {
    return (
      <span className="text-slate-500 dark:text-slate-400">
        acked
        {row.acknowledged_by && ` by ${row.acknowledged_by}`}
        {" · "}
        {timeAgo(row.acknowledged_at)}
      </span>
    );
  }

  async function ack() {
    if (!row.alert_id) return;
    setState({ status: "pending" });
    try {
      const updated = await api.ackAlert(row.alert_id);
      onAcked(updated);
    } catch (err) {
      setState({
        status: "error",
        message: err instanceof ApiError ? err.message : "ack failed",
      });
    }
  }

  return (
    <span className="flex items-center gap-1.5">
      <span className="rounded bg-amber-100 px-1 py-px text-[10px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-300">
        open
      </span>
      <button
        onClick={ack}
        disabled={state.status === "pending" || !row.alert_id}
        title={row.alert_id ? "Acknowledge this alert" : "alert has no id to ack"}
        className="rounded border border-slate-300 px-1.5 py-px text-[10px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
      >
        {state.status === "pending" ? "acking…" : "ack"}
      </button>
      {state.status === "error" && (
        <span className="text-[10px] text-red-600 dark:text-red-400">{state.message}</span>
      )}
    </span>
  );
}

function formatScore(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "?";
}
