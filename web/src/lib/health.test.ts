import { describe, expect, it } from "vitest";
import { HEALTH_COLORS, HEALTH_LABELS, healthColor, HealthState } from "./health";

describe("health mapping", () => {
  it("maps every wire state to a color and a label", () => {
    const states: HealthState[] = ["healthy", "degraded", "unreachable", "tampered", "unknown"];
    for (const state of states) {
      expect(HEALTH_COLORS[state]).toMatch(/^#/);
      expect(HEALTH_LABELS[state]).toBeTruthy();
    }
  });

  it("falls back to the unknown color for unrecognized states", () => {
    expect(healthColor("on_fire")).toBe(HEALTH_COLORS.unknown);
    expect(healthColor("")).toBe(HEALTH_COLORS.unknown);
  });

  it("returns the mapped color for known states", () => {
    expect(healthColor("healthy")).toBe(HEALTH_COLORS.healthy);
    expect(healthColor("tampered")).toBe(HEALTH_COLORS.tampered);
  });
});
