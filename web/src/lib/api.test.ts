import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

function jsonResponse(status: number, body: unknown = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("api", () => {
  const assign = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    // jsdom's location.assign would throw "not implemented: navigation" if
    // it actually ran; replace it with a spy so the redirect is observable.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, pathname: "/", search: "", assign },
    });
    assign.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("redirects to /login with a next param on a 401", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: "session expired" }));

    await expect(api.listOrgs()).rejects.toBeInstanceOf(ApiError);
    expect(assign).toHaveBeenCalledWith(`/login?next=${encodeURIComponent("/")}`);
  });

  it("does not redirect when login itself returns 401", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(401, { detail: "invalid credentials" }));

    await expect(api.login("u", "p")).rejects.toBeInstanceOf(ApiError);
    expect(assign).not.toHaveBeenCalled();
  });

  it("sends the operator's composed purpose code in X-Purpose-Code", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { plate: "GJ01AB1234", hops: [], rejected: [], dark_zones: [] }),
    );

    await api.getRoute("GJ01AB1234", "plate-trace:FIR-42/2026");

    const [, init] = vi.mocked(fetch).mock.calls[0];
    expect(new Headers(init?.headers).get("x-purpose-code")).toBe(
      "plate-trace:FIR-42/2026",
    );
  });

  it("sends the purpose code verbatim on audited camera reads", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(200, { id: "cam-1" }));

    await api.getCamera("cam-1", "investigation:FIR-9");

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe("/api/bff/cameras/cam-1");
    expect(new Headers(init?.headers).get("x-purpose-code")).toBe("investigation:FIR-9");
  });

  it("passes bbox and limit through to the geojson endpoint", async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(200, { type: "FeatureCollection", features: [] }),
    );

    await api.getCamerasGeoJSON({ bbox: "70,20,75,25", limit: 100_000 });

    const url = vi.mocked(fetch).mock.calls[0][0] as string;
    expect(url).toContain("/api/bff/cameras/geojson");
    expect(url).toContain("bbox=70%2C20%2C75%2C25");
    expect(url).toContain("limit=100000");
  });
});
