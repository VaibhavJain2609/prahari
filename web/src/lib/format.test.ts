import { describe, expect, it } from "vitest";
import { dePrefix, enumLabel, timeAgo } from "./format";

describe("timeAgo", () => {
  const now = Date.parse("2026-09-01T12:00:00Z");

  it("buckets seconds, minutes, hours and days", () => {
    expect(timeAgo("2026-09-01T11:59:30Z", now)).toBe("30s ago");
    expect(timeAgo("2026-09-01T11:30:00Z", now)).toBe("30m ago");
    expect(timeAgo("2026-09-01T06:00:00Z", now)).toBe("6h ago");
    expect(timeAgo("2026-08-28T12:00:00Z", now)).toBe("4d ago");
  });

  it("returns unparseable input verbatim instead of NaN-ing", () => {
    expect(timeAgo("not-a-date", now)).toBe("not-a-date");
  });
});

describe("enumLabel", () => {
  it("strips the given proto prefix and humanizes the tail", () => {
    expect(enumLabel("ALERT_PRIORITY_CRITICAL", "ALERT_PRIORITY_")).toBe("critical");
    expect(enumLabel("WATCHLIST_REASON_MISSING_PERSON", "WATCHLIST_REASON_")).toBe(
      "missing person",
    );
  });

  it("leaves values without the prefix humanized but whole", () => {
    expect(enumLabel("bridged")).toBe("bridged");
    expect(dePrefix("plate", "ALERT_PRIORITY_")).toBe("plate");
  });
});
