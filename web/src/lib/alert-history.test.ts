import { describe, expect, it } from "vitest";
import { StoredAlert } from "@/lib/api";
import {
  ALERT_LIST_LIMIT,
  AlertFilters,
  alertCameraId,
  alertMatchesFilters,
  alertQueryParams,
  alertRowKey,
  mergeAlertRows,
  observedPlate,
  occurredAtOf,
  plateMatches,
} from "./alert-history";

const NO_FILTERS: AlertFilters = { plate: "", camera: "", since: "", ack: "all" };

const ROW: StoredAlert = {
  id: 41,
  alert_id: "alert-1",
  dedup_key: "cam-7|GJ01AB1234|b1",
  occurred_at: "2026-09-01T10:00:00Z",
  acknowledged_at: null,
  acknowledged_by: null,
  detection: {
    camera_id: "cam-7",
    plate: { raw_text: "GJ01AB 1234", normalised_text: "GJ01AB1234" },
  },
  matched_entry: { plate: "GJ01AB1234", reason: "WATCHLIST_REASON_STOLEN" },
  explanation: { observed_plate: "GJ01AB1234", matched_plate: "GJ01AB1234", final_score: 0.98 },
};

// A live SSE frame: the Alert payload without the store's lifecycle columns.
const SSE_ROW: StoredAlert = {
  alert_id: "alert-2",
  raised_at: "2026-09-01T11:00:00Z",
  detection: {
    camera_id: "cam-9",
    plate: { normalised_text: "GJ05XY9999" },
    observed_at: { wall_clock: "2026-09-01T10:59:58Z" },
  },
  matched_entry: { plate: "GJ05XY9999" },
};

describe("alertQueryParams", () => {
  it("sends only the limit when filters are empty", () => {
    expect(alertQueryParams(NO_FILTERS)).toEqual({ limit: ALERT_LIST_LIMIT });
  });

  it("serializes camera, since (as ISO) and acknowledged", () => {
    const params = alertQueryParams({
      plate: "",
      camera: " cam-7 ",
      since: "2026-09-01T10:00",
      ack: "unacked",
    });
    expect(params.camera_id).toBe("cam-7");
    expect(params.since).toBe(new Date("2026-09-01T10:00").toISOString());
    expect(params.acknowledged).toBe(false);
    expect(params.limit).toBe(ALERT_LIST_LIMIT);
  });

  it("maps acked to acknowledged=true", () => {
    expect(alertQueryParams({ ...NO_FILTERS, ack: "acked" }).acknowledged).toBe(true);
  });

  it("keeps plate out of the query — upstream is exact-match, not substring", () => {
    const params = alertQueryParams({ ...NO_FILTERS, plate: "GJ01" });
    expect("plate" in params).toBe(false);
  });

  it("drops an unparseable since rather than sending it upstream", () => {
    expect(alertQueryParams({ ...NO_FILTERS, since: "not-a-date" }).since).toBeUndefined();
  });
});

describe("field accessors", () => {
  it("reads camera, plate and occurred_at from a persisted row", () => {
    expect(alertCameraId(ROW)).toBe("cam-7");
    expect(observedPlate(ROW)).toBe("GJ01AB1234");
    expect(occurredAtOf(ROW)).toBe("2026-09-01T10:00:00Z");
  });

  it("falls back to the detection wall clock, then raised_at, for SSE rows", () => {
    expect(occurredAtOf(SSE_ROW)).toBe("2026-09-01T10:59:58Z");
    expect(occurredAtOf({ alert_id: "x", raised_at: "2026-09-01T09:00:00Z" })).toBe(
      "2026-09-01T09:00:00Z",
    );
  });
});

describe("mergeAlertRows", () => {
  it("dedupes on alert_id — the persisted copy wins a collision", () => {
    const persistedVersion = { ...SSE_ROW, id: 99, occurred_at: "2026-09-01T10:59:59Z" };
    const { rows, liveOnly } = mergeAlertRows([persistedVersion], [SSE_ROW]);
    expect(rows).toHaveLength(1);
    expect(rows[0].id).toBe(99);
    expect(liveOnly.size).toBe(0);
  });

  it("prepends live rows and sorts newest first", () => {
    const { rows, liveOnly } = mergeAlertRows([ROW], [SSE_ROW]);
    expect(rows).toHaveLength(2);
    // SSE_ROW occurred 10:59:58 > ROW 10:00 → first.
    expect(rows[0].alert_id).toBe("alert-2");
    expect(liveOnly.has("alert:alert-2")).toBe(true);
  });

  it("keeps keyless rows distinct instead of aliasing them", () => {
    const { rows } = mergeAlertRows([{ priority: "ALERT_PRIORITY_LOW" }], [
      { priority: "ALERT_PRIORITY_HIGH" },
    ]);
    expect(rows).toHaveLength(2);
  });
});

describe("alertRowKey", () => {
  it("prefers alert_id, then row id, then dedup_key", () => {
    expect(alertRowKey(ROW, "fb")).toBe("alert:alert-1");
    expect(alertRowKey({ id: 7, dedup_key: "d" }, "fb")).toBe("row:7");
    expect(alertRowKey({ dedup_key: "d" }, "fb")).toBe("dedup:d");
    expect(alertRowKey({}, "fb")).toBe("fb");
  });
});

describe("plateMatches", () => {
  it("matches a substring case- and whitespace-insensitively", () => {
    expect(plateMatches(ROW, "gj01ab")).toBe(true);
    expect(plateMatches(ROW, "AB 1234")).toBe(true);
    expect(plateMatches(ROW, "GJ99")).toBe(false);
  });

  it("also matches the watchlist plate the alert matched on", () => {
    const row: StoredAlert = {
      detection: { plate: { normalised_text: "GJ01AB1234" } },
      matched_entry: { plate: "GJ01AB123O" },
    };
    expect(plateMatches(row, "123O")).toBe(true);
  });
});

describe("alertMatchesFilters", () => {
  it("gates on camera, ack state and since", () => {
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, camera: "cam-7" })).toBe(true);
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, camera: "cam-9" })).toBe(false);
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, ack: "unacked" })).toBe(true);
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, ack: "acked" })).toBe(false);
    // datetime-local parses as local time; use day-scale margins so the
    // assertions hold in any timezone.
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, since: "2026-08-31T00:00" })).toBe(true);
    expect(alertMatchesFilters(ROW, { ...NO_FILTERS, since: "2026-09-02T23:59" })).toBe(false);
  });

  it("treats an SSE row (no lifecycle fields) as unacknowledged", () => {
    expect(alertMatchesFilters(SSE_ROW, { ...NO_FILTERS, ack: "unacked" })).toBe(true);
    expect(alertMatchesFilters(SSE_ROW, { ...NO_FILTERS, ack: "acked" })).toBe(false);
  });
});
