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

export type RouteHop = {
  camera_id: string;
  location?: string;
  wall_clock_s?: number;
  pts_ms?: number;
  link_kind?: string;
  confidence?: number;
  evidence_ref?: string;
};

export type RouteResult = {
  plate: string;
  hops: RouteHop[];
  rejected: unknown[];
  dark_zones: unknown[];
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { purposeCode?: string } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (init.purposeCode) headers.set("x-purpose-code", init.purposeCode);

  const res = await fetch(`/api/bff/${path}`, { ...init, headers, cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(res.status, body?.detail ?? body?.error ?? `request failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function json(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

export const api = {
  me: () => request<Principal>("auth/me"),

  login: (username: string, password: string) =>
    request("auth/login", json({ username, password })),

  logout: () => request("auth/logout", { method: "POST" }),

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

  createCamera: (body: Record<string, unknown>) => request("cameras", json(body)),

  probeCamera: (body: { rtsp_url: string; username?: string; password?: string }) =>
    request<ProbeResult>("cameras/probe", { ...json(body), purposeCode: "camera-onboarding" }),

  importCameras: (csvText: string) =>
    request<ImportResult>("cameras/import", {
      method: "POST",
      body: csvText,
      headers: { "content-type": "text/csv" },
    }),

  getRoute: (plate: string) =>
    request<RouteResult>(`routes/${encodeURIComponent(plate)}`, {
      purposeCode: "plate-trace",
    }),

  // A plain <a href> can't carry the required X-Purpose-Code header, so the
  // export is a fetch that returns bytes for the caller to hand to the
  // browser's own download machinery (an object URL click, typically).
  exportRoute: async (plate: string, format: "csv" | "pdf") => {
    const res = await fetch(
      `/api/bff/routes/${encodeURIComponent(plate)}/export?format=${format}`,
      { headers: { "x-purpose-code": "plate-trace-export" }, cache: "no-store" },
    );
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new ApiError(res.status, body?.detail ?? `export failed (${res.status})`);
    }
    return res.blob();
  },

  verifyAudit: () => request<{ ok: boolean; first_broken_id: string | null }>("audit/verify"),
};
