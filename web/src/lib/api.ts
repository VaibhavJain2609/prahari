// One client for the BFF, reached through the same-origin proxy at
// /api/bff/* (see web/src/app/api/bff/[...path]/route.ts). Every board —
// global, organization, local body — calls the same functions here; the
// scoping happens server-side, in the BFF, off the caller's session. This
// file does not decide what a viewer can see.

export type Role = "viewer" | "operator" | "admin";
export type OrgKind = "state" | "organization" | "local_body";
export type ApiKeyPurpose =
  | "local_body_registration"
  | "onvif_agent"
  | "vendor_adapter"
  | "internal_service";

export type Principal = {
  id: string;
  subject: string;
  org_id: string;
  org_path: string;
  role: Role;
  kind: "session" | "api_key";
};

export type Org = {
  id: string;
  parent_id: string | null;
  path: string;
  kind: OrgKind;
  name: string;
  created_at: string | null;
};

export type ProbeResult = {
  reachable: boolean;
  transport: string;
  auth_required: boolean;
  auth_method: string | null;
  codec: string | null;
  declared_fps: number | null;
  status_message: string | null;
  sdp_media_lines: string[];
};

export type ImportRowResult = {
  row: number;
  external_id?: string;
  ok: boolean;
  id?: string;
  error?: string;
};

export type ImportResult = {
  total: number;
  succeeded: number;
  failed: number;
  rows: ImportRowResult[];
};

// Mirrors services/correlation/src/prahari_correlation/app.py::_route_to_dict.
// `location` is a GeoPoint object on the wire, not a string — rendering it
// inline crashes React 19, so it is typed honestly here and consumed by the
// map, not by a text node.
export type GeoPoint = { latitude: number; longitude: number };

export type RouteHop = {
  camera_id: string;
  location?: GeoPoint | null;
  wall_clock_s?: number;
  pts_ms?: number;
  link_kind?: "plate" | "bridged" | null;
  confidence?: number;
  evidence_ref?: string;
};

// A sighting that failed feasibility gating — excluded from `hops` and
// recorded here instead of being silently folded into the route.
export type RejectedHop = {
  from_camera_id: string;
  to_camera_id: string;
  reason: string;
  implied_speed_kmh: number | null;
};

// The registry's dark-zone list handed back alongside the route — the full
// current list, not filtered to this corridor (stated simplification in
// correlation's routes.py).
export type DarkZone = {
  camera_id: string;
  location: GeoPoint | null;
};

export type RouteResult = {
  plate: string;
  hops: RouteHop[];
  rejected: RejectedHop[];
  dark_zones: DarkZone[];
};

export type CameraGeoJSON = {
  type: "FeatureCollection";
  features: {
    type: "Feature";
    geometry: { type: "Point"; coordinates: [number, number] };
    properties: Record<string, unknown>;
  }[];
};

export type Lifecycle = "active" | "absent" | "decommissioned";

// Mirrors services/registry/src/prahari_registry/models.py::Camera — the
// REST/JSON face of the registry row. Typed in full (rather than
// Record<string, unknown>) because the drawer and the table both render it,
// and an honestly-typed field is the difference between a typo at build
// time and a blank panel at demo time. `stream_secret` is deliberately
// absent: credentials are write-only and never come back over the wire.
export type Camera = {
  id: string;
  source: string;
  external_id: string;
  location: GeoPoint | null;
  site_name: string | null;
  district: string | null;
  department: string | null;
  owner: string | null;
  org_id: string | null;
  adapter: string;
  camera_type: string;
  vendor: string | null;
  vms_platform: string | null;
  codec: string | null;
  native_width: number | null;
  native_height: number | null;
  endpoints: {
    rtsp_url: string | null;
    hls_url: string | null;
    whep_url: string | null;
    fanout_rtsp_url: string | null;
    fanout_hls_url: string | null;
    fanout_whep_url: string | null;
  };
  storage_location: string | null;
  retention_days: number | null;
  commissioned_at: string | null;
  amc_expires_at: string | null;
  lifecycle: Lifecycle;
  catalogue_live: boolean;
  present_in_catalogue: boolean;
  last_seen_in_catalogue: string | null;
  health: {
    state: string;
    reason: string | null;
    last_heartbeat_at: string | null;
    last_frame_at: string | null;
    observed_fps: number | null;
    declared_fps: number | null;
    fps_drift: number | null;
    black_frame_ratio: number | null;
    tamper_suspected: boolean;
    consecutive_failures: number;
    loop_epoch: number;
    last_error: string | null;
  };
  created_at: string | null;
  updated_at: string | null;
};

// services/registry .../app.py::list_cameras — the filters the registry
// understands. `lifecycle` is single-valued server-side; "all" is the
// caller's problem (three fetches, merged — see CamerasTable).
export type CameraListParams = {
  district?: string;
  department?: string;
  state?: string;
  lifecycle?: Lifecycle;
  search?: string;
  limit?: number;
  offset?: number;
};

// cameras/summary — headline counts plus a per-health-state breakdown of
// the active estate (see repository.py::health_summary).
export type CamerasSummary = {
  active: number;
  absent: number;
  decommissioned: number;
  health: Record<string, number>;
};

// gaps/districts — DistrictCoverage. `district` is null for the row that
// aggregates cameras with no district recorded.
export type DistrictCoverage = {
  district: string | null;
  registered: number;
  healthy: number;
  degraded: number;
  unreachable: number;
  tampered: number;
  unknown: number;
  absent: number;
  coverage_pct: number;
};

// gaps/dark-zones — a camera that is down with no healthy camera near
// enough to cover for it. `nearest_healthy_m` null means no healthy camera
// with a known location exists anywhere in scope.
export type DarkZoneInfo = {
  camera_id: string;
  site_name: string | null;
  district: string | null;
  location: GeoPoint | null;
  state: string;
  reason: string | null;
  nearest_healthy_m: number | null;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type ApiInit = RequestInit & {
  purposeCode?: string;
  // login/logout must not trip the global 401 bounce — a failed login IS a
  // 401, and redirecting to /login from /login would loop.
  skipAuthRedirect?: boolean;
};

// A 401 anywhere in the console means the session is gone; send the operator
// to /login with a `next` back to where they were. Browser-only: on the
// server there is no window to navigate and no session cookie to lose.
function redirectToLogin() {
  if (typeof window === "undefined") return;
  const here = window.location.pathname + window.location.search;
  // Not a component — useRouter isn't reachable from a fetch wrapper, and a
  // hard navigation is what we want anyway: the dead session's client state
  // should not survive the trip to /login.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign(`/login?next=${encodeURIComponent(here)}`);
}

async function request<T>(path: string, init: ApiInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (init.purposeCode) headers.set("x-purpose-code", init.purposeCode);

  const res = await fetch(`/api/bff/${path}`, { ...init, headers, cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const error = new ApiError(
      res.status,
      body?.detail ?? body?.error ?? `request failed (${res.status})`,
    );
    if (res.status === 401 && !init.skipAuthRedirect) redirectToLogin();
    throw error;
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function json(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

// Audited actions carry a purpose code of the form "<action>:<case ref>" —
// the case reference is operator-supplied (a FIR/eGujCop number, an incident
// id, …) so the hash-chained audit log records why the access happened, not
// just that it did. A blank ref degrades gracefully to the bare action code.
function purpose(action: string, caseRef?: string): string {
  const ref = caseRef?.trim();
  return ref ? `${action}:${ref}` : action;
}

export const api = {
  // Escape hatch for endpoints that don't need a named method yet —
  // useBFF(path) calls this. Same-origin proxy, same 401 handling.
  get: <T>(path: string, init: ApiInit = {}) => request<T>(path, init),

  me: () => request<Principal>("auth/me"),

  login: (username: string, password: string) =>
    request("auth/login", { ...json({ username, password }), skipAuthRedirect: true }),

  logout: () => request("auth/logout", { method: "POST", skipAuthRedirect: true }),

  listOrgs: () => request<Org[]>("orgs"),

  createOrg: (body: { parent_id?: string; label: string; kind: OrgKind; name: string }) =>
    request<Org>("orgs", json(body)),

  createUser: (body: { username: string; password: string; org_id: string; role: Role }) =>
    request("auth/users", json(body)),

  createApiKey: (body: {
    org_id: string;
    role: Role;
    purpose: ApiKeyPurpose;
    label: string;
  }) => request<{ plaintext: string }>("auth/api-keys", json(body)),

  // The scoped camera feed. `bbox` is "min_lon,min_lat,max_lon,max_lat" in
  // the registry's terms; `limit` caps at the registry's 100k ceiling, and a
  // FeatureCollection that hits it is silently truncated — the caller shows
  // the count with a ≥ caveat rather than pretending it is complete.
  getCamerasGeoJSON: (opts: { bbox?: string; limit?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.bbox) params.set("bbox", opts.bbox);
    if (opts.limit != null) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return request<CameraGeoJSON>(`cameras/geojson${qs ? `?${qs}` : ""}`);
  },

  listCameras: (params: CameraListParams = {}, init: ApiInit = {}) => {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value != null && value !== "") qs.set(key, String(value));
    }
    const query = qs.toString();
    return request<Camera[]>(`cameras${query ? `?${query}` : ""}`, init);
  },

  camerasSummary: () => request<CamerasSummary>("cameras/summary"),

  gapsDistricts: () => request<DistrictCoverage[]>("gaps/districts"),

  gapsDarkZones: (radiusM?: number) =>
    request<DarkZoneInfo[]>(
      `gaps/dark-zones${radiusM != null ? `?radius_m=${radiusM}` : ""}`,
    ),

  // Camera detail is an audited read of a specific, identifiable asset —
  // the BFF requires a purpose code and writes it to the audit log. The
  // caller passes the operator's composed purpose (lib/purpose.tsx), not a
  // bare case ref: the code records *why* the read happened, while the
  // audit entry's own action/resource fields record *what* was read.
  getCamera: (cameraId: string, purposeCode: string) =>
    request<Camera>(`cameras/${encodeURIComponent(cameraId)}`, {
      purposeCode,
    }),

  updateCamera: (cameraId: string, body: Record<string, unknown>) =>
    request<Camera>(`cameras/${encodeURIComponent(cameraId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  decommissionCamera: (cameraId: string) =>
    request<Camera>(`cameras/${encodeURIComponent(cameraId)}`, { method: "DELETE" }),

  createCamera: (body: Record<string, unknown>) => request("cameras", json(body)),

  probeCamera: (
    body: { rtsp_url: string; username?: string; password?: string },
    caseRef?: string,
  ) =>
    request<ProbeResult>("cameras/probe", {
      ...json(body),
      purposeCode: purpose("camera-onboarding", caseRef),
    }),

  importCameras: (csvText: string) =>
    request<ImportResult>("cameras/import", {
      method: "POST",
      body: csvText,
      headers: { "content-type": "text/csv" },
    }),

  // `purposeCode` is the operator-composed code (lib/purpose.tsx —
  // "<action>:<case ref>"), sent verbatim: the operator chooses why the
  // trace runs, and a client that silently rewrote it would be decorating
  // the audit log rather than filling it.
  getRoute: (plate: string, purposeCode: string) =>
    request<RouteResult>(`routes/${encodeURIComponent(plate)}`, {
      purposeCode,
    }),

  // A plain <a href> can't carry the required X-Purpose-Code header, so the
  // export is a fetch that returns bytes for the caller to hand to the
  // browser's own download machinery (an object URL click, typically). The
  // same purpose code that produced the trace threads through to the export.
  exportRoute: async (plate: string, format: "csv" | "pdf", purposeCode: string) => {
    const res = await fetch(
      `/api/bff/routes/${encodeURIComponent(plate)}/export?format=${format}`,
      {
        headers: { "x-purpose-code": purposeCode },
        cache: "no-store",
      },
    );
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      if (res.status === 401) redirectToLogin();
      throw new ApiError(res.status, body?.detail ?? `export failed (${res.status})`);
    }
    return res.blob();
  },

  // Response keys mirror the BFF's verify_audit handler: `ok` plus the id
  // of the first entry whose hash doesn't chain, if any.
  verifyAudit: () =>
    request<{ ok: boolean; first_broken_entry: string | null }>("audit/verify"),
};
