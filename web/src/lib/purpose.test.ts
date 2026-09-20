import { describe, expect, it } from "vitest";
import { composePurpose } from "./purpose";

describe("composePurpose", () => {
  it("composes action:ref", () => {
    expect(composePurpose({ action: "investigation", ref: "FIR-9" })).toBe(
      "investigation:FIR-9",
    );
  });

  it("degrades to the bare action on a blank ref", () => {
    expect(composePurpose({ action: "court-order", ref: "  " })).toBe("court-order");
  });

  it("is null without a purpose", () => {
    expect(composePurpose(null)).toBeNull();
  });
});
