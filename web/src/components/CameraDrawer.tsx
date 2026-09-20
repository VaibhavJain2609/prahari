"use client";

import { useState } from "react";
import { api, ApiError, Camera } from "@/lib/api";
import { useBFF } from "@/lib/use-bff";
import { usePrincipal } from "@/lib/principal";
import { composePurpose, PurposePrompt, usePurpose } from "@/lib/purpose";
import { can } from "@/lib/rbac";
import { timeAgo } from "@/lib/format";
import { HealthBadge } from "@/components/ui/Badge";

// The camera detail surface, driven by `?camera=<id>` so a selection is a
// shareable URL and survives the map's clustering. The detail read is
// audited — the BFF refuses without X-Purpose-Code — so an unset session
// purpose prompts inline before any fetch happens.
export default function CameraDrawer({
  cameraId,
  onClose,
  onChanged,
}: {
  cameraId: string;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const { principal } = usePrincipal();
  const { purpose } = usePurpose();
  const code = composePurpose(purpose);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Gated on the purpose code: without one the call can only 400.
  const { data: camera, error, loading, revalidate } = useBFF<Camera>(
    code ? `cameras/${encodeURIComponent(cameraId)}` : null,
    { purposeCode: code },
  );

  // Health-history slot: the endpoint is planned, not yet shipped — a 404
  // is the expected answer today and renders as a quiet placeholder, not an
  // error.
  const history = useBFF<Record<string, unknown>[]>(
    code ? `cameras/${encodeURIComponent(cameraId)}/health-history` : null,
    { purposeCode: code ?? undefined },
  );

  async function onDecommission() {
    setBusy(true);
    setActionError(null);
    try {
      await api.decommissionCamera(cameraId);
      onChanged?.();
      onClose();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "decommission failed");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="absolute bottom-0 right-0 top-0 z-10 flex w-96 flex-col border-l border-slate-200 bg-white shadow-lg dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2 dark:border-slate-800">
        <h2 className="truncate text-sm font-semibold text-slate-800 dark:text-slate-100">
          {camera?.site_name || camera?.external_id || cameraId}
        </h2>
        <button
          onClick={onClose}
          aria-label="Close camera detail"
          className="rounded px-1.5 py-0.5 text-xs text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800"
        >
          ✕
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 text-xs">
        {!purpose ? (
          <PurposePrompt />
        ) : loading && !camera ? (
          <p className="animate-pulse text-slate-400">loading camera…</p>
        ) : error ? (
          <p className="text-red-600 dark:text-red-400">
            {error.status === 403
              ? "This camera is outside your org subtree."
              : error.status === 404
                ? "No such camera."
                : error.message}
          </p>
        ) : camera ? (
          editing ? (
            <EditForm
              camera={camera}
              onDone={(changed) => {
                setEditing(false);
                if (changed) revalidate();
                if (changed) onChanged?.();
              }}
            />
          ) : (
            <div className="space-y-3">
              <Section title="Identity">
                <Row k="External ID" v={camera.external_id} mono />
                <Row k="Site" v={camera.site_name} />
                <Row k="District" v={camera.district} />
                <Row k="Department" v={camera.department} />
                <Row k="Owner" v={camera.owner} />
                <Row k="Type" v={camera.camera_type} />
                <Row k="Vendor" v={camera.vendor} />
                <Row k="VMS" v={camera.vms_platform} />
                <Row k="Adapter" v={camera.adapter} />
                <Row k="Source" v={camera.source} />
                <Row
                  k="Location"
                  v={
                    camera.location
                      ? `${camera.location.latitude.toFixed(5)}, ${camera.location.longitude.toFixed(5)}`
                      : null
                  }
                  mono
                />
                <Row k="Org" v={camera.org_id} mono />
              </Section>

              <Section title="Health">
                <div className="flex items-center gap-2">
                  <HealthBadge state={camera.health.state} />
                  {camera.health.reason && (
                    <span className="text-slate-500 dark:text-slate-400">
                      {camera.health.reason}
                    </span>
                  )}
                </div>
                <Row
                  k="Last frame"
                  v={camera.health.last_frame_at ? timeAgo(camera.health.last_frame_at) : "never"}
                />
                <Row
                  k="Heartbeat"
                  v={
                    camera.health.last_heartbeat_at
                      ? timeAgo(camera.health.last_heartbeat_at)
                      : "never"
                  }
                />
                <Row
                  k="FPS"
                  v={
                    camera.health.observed_fps != null
                      ? `${camera.health.observed_fps.toFixed(1)} observed${
                          camera.health.declared_fps != null
                            ? ` / ${camera.health.declared_fps} declared`
                            : ""
                        }`
                      : null
                  }
                />
                {camera.health.tamper_suspected && (
                  <p className="text-red-600 dark:text-red-400">tamper suspected</p>
                )}
                <Row k="Last error" v={camera.health.last_error} />
                {/* Health-history slot: renders nothing while the endpoint
                    is unshipped (404 tolerated inside useBFF's result). */}
                {history.data && history.data.length > 0 ? (
                  <p className="text-slate-500 dark:text-slate-400">
                    {history.data.length} historical entries.
                  </p>
                ) : (
                  !history.loading && (
                    <p className="text-slate-400 dark:text-slate-500">
                      health history not available
                    </p>
                  )
                )}
              </Section>

              <Section title="Streams">
                {camera.endpoints.fanout_whep_url ? (
                  <a
                    href={camera.endpoints.fanout_whep_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sky-600 underline focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-sky-400"
                  >
                    Live preview (WHEP)
                  </a>
                ) : (
                  <p className="text-slate-400 dark:text-slate-500">no browser preview</p>
                )}
                {camera.endpoints.fanout_hls_url && (
                  <Row k="HLS" v={camera.endpoints.fanout_hls_url} mono />
                )}
              </Section>

              <Section title="Lifecycle">
                <Row k="State" v={camera.lifecycle} />
                <Row k="In catalogue" v={camera.present_in_catalogue ? "yes" : "no"} />
                <Row
                  k="Last seen"
                  v={
                    camera.last_seen_in_catalogue
                      ? timeAgo(camera.last_seen_in_catalogue)
                      : null
                  }
                />
                <Row
                  k="Commissioned"
                  v={camera.commissioned_at?.slice(0, 10) ?? null}
                />
                <Row k="AMC expires" v={camera.amc_expires_at?.slice(0, 10) ?? null} />
                <Row
                  k="Retention"
                  v={camera.retention_days != null ? `${camera.retention_days} days` : null}
                />
                <Row k="Storage" v={camera.storage_location} />
              </Section>

              {can(principal, "operator") && camera.lifecycle !== "decommissioned" && (
                <div className="flex gap-2 border-t border-slate-200 pt-2 dark:border-slate-800">
                  <button
                    onClick={() => setEditing(true)}
                    className="rounded border border-slate-300 px-2 py-1 text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                  >
                    Edit
                  </button>
                  {confirming ? (
                    <>
                      <button
                        onClick={onDecommission}
                        disabled={busy}
                        className="rounded bg-red-600 px-2 py-1 font-medium text-white focus:outline-none focus:ring-2 focus:ring-red-400 disabled:opacity-50"
                      >
                        {busy ? "…" : "Confirm decommission"}
                      </button>
                      <button
                        onClick={() => setConfirming(false)}
                        className="rounded border border-slate-300 px-2 py-1 text-slate-700 dark:border-slate-700 dark:text-slate-300"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setConfirming(true)}
                      className="rounded border border-red-300 px-2 py-1 text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-400 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/40"
                    >
                      Decommission
                    </button>
                  )}
                </div>
              )}
              {actionError && <p className="text-red-600 dark:text-red-400">{actionError}</p>}
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        {title}
      </h3>
      <div className="space-y-0.5">{children}</div>
    </section>
  );
}

function Row({ k, v, mono }: { k: string; v: string | null | undefined; mono?: boolean }) {
  if (v == null || v === "") return null;
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-500 dark:text-slate-400">{k}</span>
      <span
        className={`truncate text-right text-slate-800 dark:text-slate-200 ${mono ? "font-mono text-[11px]" : ""}`}
      >
        {v}
      </span>
    </div>
  );
}

// CameraUpdate semantics: only fields the operator actually changed are
// sent — the registry writes exactly what arrives, so echoing untouched
// fields back would blank nothing but would also make the PATCH lie about
// what the operator meant to edit.
function EditForm({
  camera,
  onDone,
}: {
  camera: Camera;
  onDone: (changed: boolean) => void;
}) {
  const [form, setForm] = useState({
    site_name: camera.site_name ?? "",
    district: camera.district ?? "",
    department: camera.department ?? "",
    owner: camera.owner ?? "",
    vendor: camera.vendor ?? "",
    vms_platform: camera.vms_platform ?? "",
    camera_type: camera.camera_type ?? "unspecified",
    storage_location: camera.storage_location ?? "",
    retention_days: camera.retention_days != null ? String(camera.retention_days) : "",
    latitude: camera.location ? String(camera.location.latitude) : "",
    longitude: camera.location ? String(camera.location.longitude) : "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function field(key: keyof typeof form) {
    return {
      value: form[key],
      onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
        setForm((f) => ({ ...f, [key]: e.target.value })),
    };
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    const str = (key: keyof typeof form, current: string | null) => {
      const v = form[key].trim();
      if (v !== (current ?? "")) body[key] = v === "" ? null : v;
    };
    str("site_name", camera.site_name);
    str("district", camera.district);
    str("department", camera.department);
    str("owner", camera.owner);
    str("vendor", camera.vendor);
    str("vms_platform", camera.vms_platform);
    str("storage_location", camera.storage_location);
    if (form.camera_type !== camera.camera_type) body.camera_type = form.camera_type;
    const retention = form.retention_days.trim();
    if (retention !== (camera.retention_days != null ? String(camera.retention_days) : "")) {
      body.retention_days = retention === "" ? null : Number(retention);
    }
    const lat = form.latitude.trim();
    const lon = form.longitude.trim();
    if (
      lat !== (camera.location ? String(camera.location.latitude) : "") ||
      lon !== (camera.location ? String(camera.location.longitude) : "")
    ) {
      body.location =
        lat === "" || lon === ""
          ? null
          : { latitude: Number(lat), longitude: Number(lon) };
    }
    if (Object.keys(body).length === 0) {
      onDone(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.updateCamera(camera.id, body);
      onDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "update failed");
    } finally {
      setBusy(false);
    }
  }

  const input =
    "w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  return (
    <form onSubmit={onSubmit} className="space-y-2">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        Edit camera
      </p>
      <input {...field("site_name")} placeholder="site name" aria-label="Site name" className={input} />
      <div className="flex gap-2">
        <input {...field("district")} placeholder="district" aria-label="District" className={input} />
        <input {...field("department")} placeholder="department" aria-label="Department" className={input} />
      </div>
      <input {...field("owner")} placeholder="owner" aria-label="Owner" className={input} />
      <div className="flex gap-2">
        <input {...field("latitude")} placeholder="latitude" aria-label="Latitude" className={input} />
        <input {...field("longitude")} placeholder="longitude" aria-label="Longitude" className={input} />
      </div>
      <select {...field("camera_type")} aria-label="Camera type" className={input}>
        <option value="unspecified">unspecified</option>
        <option value="analog">analog</option>
        <option value="ip">ip</option>
        <option value="ptz">ptz</option>
        <option value="anpr">anpr</option>
      </select>
      <div className="flex gap-2">
        <input {...field("vendor")} placeholder="vendor" aria-label="Vendor" className={input} />
        <input {...field("vms_platform")} placeholder="vms platform" aria-label="VMS platform" className={input} />
      </div>
      <div className="flex gap-2">
        <input
          {...field("retention_days")}
          placeholder="retention days"
          aria-label="Retention days"
          inputMode="numeric"
          className={input}
        />
        <input
          {...field("storage_location")}
          placeholder="storage location"
          aria-label="Storage location"
          className={input}
        />
      </div>
      {error && <p className="text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
        >
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={() => onDone(false)}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 dark:border-slate-700 dark:text-slate-300"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
