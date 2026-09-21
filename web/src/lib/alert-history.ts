// Helpers behind the /alerts work surface — pure and unit-tested so the
// component stays about rendering. Three jobs: serialize the filter bar
// into BFF query params, pull display fields out of the loosely-typed
// StoredAlert payload, and merge the persisted page with live SSE rows
// without ever showing one alert twice.

import { AlertListParams, StoredAlert } from "@/lib/api";

export type AlertFilters = {
  // Substring over the observed OR matched plate. The BFF's `plate` param
  // is exact-match upstream (match-engine `plate = $ OR matched_plate = $`)
  // so it stays out of the query — the filter is applied to the fetched
  // window client-side, which is also what lets live SSE rows participate.
  plate: string;
  // Exact camera id, server-side.
  camera: string;
  // `datetime-local` value ("YYYY-MM-DDTHH:mm") — local wall clock, turned
  // into an ISO instant for the query. Empty means unbounded.
  since: string;
  ack: "all" | "unacked" | "acked";
};

export const ALERT_LIST_LIMIT = 200;

// The params GET /api/bff/alerts actually receives. `since` is sent as a
// UTC ISO instant; a datetime-local string that doesn't parse is dropped
// rather than sent upstream to 422.
export function alertQueryParams(filters: AlertFilters): AlertListParams {
  let since: string | undefined;
  const ms = new Date(filters.since).getTime();
  if (filters.since && Number.isFinite(ms)) since = new Date(ms).toISOString();
  return {
    camera_id: filters.camera.trim() || undefined,
    since,
    acknowledged:
      filters.ack === "all" ? undefined : filters.ack === "acked" ? true : false,
    limit: ALERT_LIST_LIMIT,
  };
}

function detectionOf(row: StoredAlert): Record<string, unknown> {
  return (row.detection ?? {}) as Record<string, unknown>;
}

export function alertCameraId(row: StoredAlert): string | null {
  return (detectionOf(row).camera_id as string) || null;
}

// What an officer searches for: the plate as observed, never the corrected
// match (inference never corrects — the match explanation does).
export function observedPlate(row: StoredAlert): string | null {
  const explanation = (row.explanation ?? {}) as Record<string, unknown>;
  const plate = (detectionOf(row).plate ?? {}) as Record<string, unknown>;
  return (
    (explanation.observed_plate as string) ??
    (plate.normalised_text as string) ??
    (plate.raw_text as string) ??
    null
  );
}

export function rawPlateText(row: StoredAlert): string | null {
  const plate = (detectionOf(row).plate ?? {}) as Record<string, unknown>;
  return (plate.raw_text as string) ?? null;
}

export function matchedPlate(row: StoredAlert): string | null {
  const matched = (row.matched_entry ?? {}) as Record<string, unknown>;
  const explanation = (row.explanation ?? {}) as Record<string, unknown>;
  return (matched.plate as string) ?? (explanation.matched_plate as string) ?? null;
}

// History answers "when was this vehicle seen": the persisted row's
// occurred_at, else the detection's wall clock, else raised_at — the same
// fallback chain as match-engine's alert_to_record.
export function occurredAtOf(row: StoredAlert): string | null {
  const observed = (detectionOf(row).observed_at ?? {}) as Record<string, unknown>;
  return row.occurred_at ?? (observed.wall_clock as string) ?? row.raised_at ?? null;
}

function occurredMs(row: StoredAlert): number {
  const ms = new Date(occurredAtOf(row) ?? "").getTime();
  return Number.isFinite(ms) ? ms : 0;
}

// The dedupe identity. `alert_id` is assigned by the match engine and is
// identical between the SSE frame and the persisted row, which is what
// makes "SSE arrived, then refresh landed" collapse to one row. Rows
// without one (a malformed frame, a synthetic test row) fall back to the
// store's row id, then the dedup key, then the caller's fallback so two
// keyless rows never alias each other.
export function alertRowKey(row: StoredAlert, fallback: string): string {
  if (row.alert_id) return `alert:${row.alert_id}`;
  if (row.id != null) return `row:${row.id}`;
  if (row.dedup_key) return `dedup:${row.dedup_key}`;
  return fallback;
}

// Client-side predicate shared by the render path and the SSE gate — a
// live alert that doesn't match the current filter set must not pop into
// a filtered view just because it happened now.
export function alertMatchesFilters(row: StoredAlert, filters: AlertFilters): boolean {
  if (filters.ack === "acked" && !row.acknowledged_at) return false;
  if (filters.ack === "unacked" && row.acknowledged_at) return false;
  const camera = filters.camera.trim();
  if (camera && alertCameraId(row) !== camera) return false;
  if (!plateMatches(row, filters.plate)) return false;
  if (filters.since) {
    const sinceMs = new Date(filters.since).getTime();
    if (Number.isFinite(sinceMs) && occurredMs(row) < sinceMs) return false;
  }
  return true;
}

// Substring over observed plate, matched plate, and the raw OCR text —
// whitespace/hyphen/case-insensitive, matching the plate grammar's
// normalisation conventions.
export function plateMatches(row: StoredAlert, needle: string): boolean {
  const n = needle.toUpperCase().replace(/[\s-]/g, "");
  if (!n) return true;
  const normalize = (p: string) => p.toUpperCase().replace(/[\s-]/g, "");
  return [observedPlate(row), matchedPlate(row), rawPlateText(row)].some(
    (p) => p != null && normalize(p).includes(n),
  );
}

// Newest-first merge of the persisted page with live SSE rows, deduped on
// alert_id. The persisted copy wins a collision: it carries the ack
// lifecycle fields the SSE frame lacks. `liveOnly` is the set of keys only
// the stream produced — what the UI highlights as "new".
export function mergeAlertRows(
  persisted: StoredAlert[],
  live: StoredAlert[],
): { rows: StoredAlert[]; liveOnly: Set<string> } {
  const byKey = new Map<string, StoredAlert>();
  persisted.forEach((row, i) => byKey.set(alertRowKey(row, `persisted-${i}`), row));
  const liveOnly = new Set<string>();
  live.forEach((row, i) => {
    const key = alertRowKey(row, `live-${i}`);
    if (!byKey.has(key)) {
      byKey.set(key, row);
      liveOnly.add(key);
    }
  });
  const rows = [...byKey.values()].sort((a, b) => occurredMs(b) - occurredMs(a));
  return { rows, liveOnly };
}
