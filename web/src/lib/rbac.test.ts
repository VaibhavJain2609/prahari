import { describe, expect, it } from "vitest";
import { can } from "./rbac";
import { Principal } from "./api";

const principal = (role: Principal["role"]): Principal => ({
  id: "u1",
  subject: "op1",
  org_id: "org-1",
  org_path: "gj.org",
  role,
  kind: "session",
});

describe("can", () => {
  it("orders roles viewer < operator < admin", () => {
    expect(can(principal("viewer"), "viewer")).toBe(true);
    expect(can(principal("viewer"), "operator")).toBe(false);
    expect(can(principal("viewer"), "admin")).toBe(false);
    expect(can(principal("operator"), "viewer")).toBe(true);
    expect(can(principal("operator"), "operator")).toBe(true);
    expect(can(principal("operator"), "admin")).toBe(false);
    expect(can(principal("admin"), "admin")).toBe(true);
  });

  it("denies everything to a null principal", () => {
    expect(can(null, "viewer")).toBe(false);
    expect(can(undefined, "admin")).toBe(false);
  });
});
