"use client";

import { useState } from "react";
import { api, ApiError, ProbeResult } from "@/lib/api";

// Manual registration (was OnboardingPanel's ManualAdd, extended to the
// full CameraCreate field set). Probe-before-save is preserved: the probe
// hits the operator-supplied RTSP URL and is itself an audited call, so the
// optional case ref rides along. org_id is deliberately *not* a form field
// — the BFF scopes new cameras to the caller's own subtree; an operator is
// never trusted to type their own scope.
export default function CameraRegisterForm({ onRegistered }: { onRegistered?: () => void }) {
  const [form, setForm] = useState({
    external_id: "",
    site_name: "",
    district: "",
    department: "",
    owner: "",
    latitude: "",
    longitude: "",
    adapter: "manual",
    camera_type: "unspecified",
    vendor: "",
    vms_platform: "",
    codec: "",
    native_width: "",
    native_height: "",
    declared_fps: "",
    rtsp_url: "",
    hls_url: "",
    whep_url: "",
    stream_username: "",
    stream_password: "",
    storage_location: "",
    retention_days: "",
    commissioned_at: "",
    amc_expires_at: "",
    stale_after_s: "",
  });
  const [caseRef, setCaseRef] = useState("");
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  function field(key: keyof typeof form) {
    return {
      value: form[key],
      onChange: (
        e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>,
      ) => setForm((f) => ({ ...f, [key]: e.target.value })),
    };
  }

  const input =
    "w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  async function onProbe() {
    if (!form.rtsp_url.trim()) return;
    setProbing(true);
    setMessage(null);
    setProbe(null);
    try {
      setProbe(
        await api.probeCamera(
          {
            rtsp_url: form.rtsp_url.trim(),
            username: form.stream_username || undefined,
            password: form.stream_password || undefined,
          },
          caseRef.trim() || undefined,
        ),
      );
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "probe failed" });
    } finally {
      setProbing(false);
    }
  }

  async function onSave(e: React.FormEvent) {
    e.preventDefault();
    if (!form.external_id.trim() || !form.site_name.trim()) return;
    setSaving(true);
    setMessage(null);
    // Only non-empty fields go on the wire — the registry treats absent as
    // unset, and a blank string written is a blank string stored.
    const body: Record<string, unknown> = {
      source: "manual",
      external_id: form.external_id.trim(),
      site_name: form.site_name.trim(),
    };
    const opt = (key: keyof typeof form, name?: string) => {
      const v = form[key].trim();
      if (v !== "") body[name ?? key] = v;
    };
    opt("district");
    opt("department");
    opt("owner");
    opt("adapter");
    opt("vendor");
    opt("vms_platform");
    opt("codec");
    opt("rtsp_url");
    opt("hls_url");
    opt("whep_url");
    opt("stream_username");
    opt("stream_password");
    opt("storage_location");
    if (form.camera_type !== "unspecified") body.camera_type = form.camera_type;
    const num = (key: keyof typeof form, name?: string) => {
      const v = form[key].trim();
      if (v !== "") body[name ?? key] = Number(v);
    };
    num("native_width");
    num("native_height");
    num("declared_fps");
    num("retention_days");
    num("stale_after_s");
    if (form.latitude.trim() !== "" && form.longitude.trim() !== "") {
      body.location = {
        latitude: Number(form.latitude),
        longitude: Number(form.longitude),
      };
    }
    // <input type="date|datetime-local"> yields a local date stamp; the API
    // wants an instant — convert or drop the field rather than send a
    // silently-misinterpreted string.
    if (form.commissioned_at) {
      const d = new Date(form.commissioned_at);
      if (!Number.isNaN(d.getTime())) body.commissioned_at = d.toISOString();
    }
    if (form.amc_expires_at) {
      const d = new Date(form.amc_expires_at);
      if (!Number.isNaN(d.getTime())) body.amc_expires_at = d.toISOString();
    }
    try {
      await api.createCamera(body);
      setMessage({ ok: true, text: `${form.external_id} registered.` });
      setForm((f) =>
        Object.fromEntries(Object.keys(f).map((k) => [k, k === "adapter" ? "manual" : k === "camera_type" ? "unspecified" : ""])) as typeof f,
      );
      setProbe(null);
      onRegistered?.();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "save failed" });
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={onSave} className="max-h-[70vh] space-y-3 overflow-y-auto pr-1">
      <Section title="Identity">
        <div className="flex gap-2">
          <input {...field("external_id")} placeholder="external id *" aria-label="External ID (required)" className={input} />
          <input {...field("site_name")} placeholder="site name *" aria-label="Site name (required)" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("district")} placeholder="district" aria-label="District" className={input} />
          <input {...field("department")} placeholder="department" aria-label="Department" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("owner")} placeholder="owner" aria-label="Owner" className={input} />
          <input {...field("adapter")} placeholder="adapter" aria-label="Adapter" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("latitude")} placeholder="latitude" aria-label="Latitude" inputMode="decimal" className={input} />
          <input {...field("longitude")} placeholder="longitude" aria-label="Longitude" inputMode="decimal" className={input} />
        </div>
      </Section>

      <Section title="Stream">
        <input {...field("rtsp_url")} placeholder="rtsp://..." aria-label="RTSP URL" className={input} />
        <div className="flex gap-2">
          <input {...field("hls_url")} placeholder="hls url" aria-label="HLS URL" className={input} />
          <input {...field("whep_url")} placeholder="whep url" aria-label="WHEP URL" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("stream_username")} placeholder="stream username" aria-label="Stream username" className={input} />
          <input type="password" {...field("stream_password")} placeholder="stream password" aria-label="Stream password" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("codec")} placeholder="codec" aria-label="Codec" className={input} />
          <input {...field("declared_fps")} placeholder="declared fps" aria-label="Declared FPS" inputMode="decimal" className={input} />
        </div>
      </Section>

      <Section title="Equipment">
        <div className="flex gap-2">
          <select {...field("camera_type")} aria-label="Camera type" className={input}>
            <option value="unspecified">type: unspecified</option>
            <option value="analog">analog</option>
            <option value="ip">ip</option>
            <option value="ptz">ptz</option>
            <option value="anpr">anpr</option>
          </select>
          <input {...field("vendor")} placeholder="vendor" aria-label="Vendor" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("vms_platform")} placeholder="vms platform" aria-label="VMS platform" className={input} />
        </div>
        <div className="flex gap-2">
          <input {...field("native_width")} placeholder="native width" aria-label="Native width" inputMode="numeric" className={input} />
          <input {...field("native_height")} placeholder="native height" aria-label="Native height" inputMode="numeric" className={input} />
        </div>
      </Section>

      <Section title="Meta">
        <div className="flex gap-2">
          <input {...field("storage_location")} placeholder="storage location" aria-label="Storage location" className={input} />
          <input {...field("retention_days")} placeholder="retention days" aria-label="Retention days" inputMode="numeric" className={input} />
        </div>
        <div className="flex gap-2">
          <label className="flex-1">
            <span className="mb-0.5 block text-[10px] text-slate-400">commissioned</span>
            <input type="date" {...field("commissioned_at")} aria-label="Commissioned at" className={input} />
          </label>
          <label className="flex-1">
            <span className="mb-0.5 block text-[10px] text-slate-400">amc expires</span>
            <input type="date" {...field("amc_expires_at")} aria-label="AMC expires at" className={input} />
          </label>
        </div>
        <input {...field("stale_after_s")} placeholder="stale after (s)" aria-label="Stale after seconds" inputMode="numeric" className={input} />
      </Section>

      <input
        value={caseRef}
        onChange={(e) => setCaseRef(e.target.value)}
        placeholder="case reference (optional — appended to probe's audit purpose)"
        aria-label="Case reference for probe audit (optional)"
        className={input}
      />

      <div className="flex gap-2">
        <button
          type="button"
          onClick={onProbe}
          disabled={probing || !form.rtsp_url.trim()}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800 dark:focus:ring-slate-500"
        >
          {probing ? "Probing…" : "Probe"}
        </button>
        <button
          type="submit"
          disabled={saving || !form.external_id.trim() || !form.site_name.trim()}
          className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>

      {probe && (
        <div className="rounded border border-slate-200 px-2 py-1 text-[11px] text-slate-600 dark:border-slate-800 dark:text-slate-400">
          {probe.reachable ? "reachable" : "unreachable"}
          {probe.codec ? ` · ${probe.codec}` : ""}
          {probe.declared_fps != null ? ` · ${probe.declared_fps} fps (declared)` : ""}
          {probe.status_message ? ` — ${probe.status_message}` : ""}
        </div>
      )}
      {message && (
        <p className={`text-xs ${message.ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset className="space-y-1.5">
      <legend className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        {title}
      </legend>
      {children}
    </fieldset>
  );
}
