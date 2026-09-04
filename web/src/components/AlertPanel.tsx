"use client";

import { useEffect, useRef, useState } from "react";

type AlertItem = {
  id: string;
  receivedAt: number;
  raw: Record<string, unknown>;
};

type StreamState = "connecting" | "open" | "unavailable" | "error";

// Live relay off the BFF's SSE endpoint, same-origin through the proxy so
// the session cookie attaches automatically. The BFF already scopes every
// alert to the caller's org subtree (org_path_for_camera + in_scope) before
// it reaches the wire, so this panel does no filtering of its own — it only
// renders what arrives.
export default function AlertPanel() {
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const source = new EventSource("/api/bff/alerts/stream");
    sourceRef.current = source;

    source.onopen = () => setState("open");

    source.addEventListener("alert", (event) => {
      const raw = JSON.parse((event as MessageEvent).data);
      setAlerts((prev) => [{ id: crypto.randomUUID(), receivedAt: Date.now(), raw }, ...prev].slice(0, 20));
    });

    source.onerror = () => {
      // The BFF returns 503 (no redis_url configured) as a plain HTTP
      // error before any SSE framing starts, which EventSource surfaces
      // here indistinguishably from a dropped connection — both read as
      // "not currently available" from this panel's point of view.
      setState(source.readyState === EventSource.CLOSED ? "unavailable" : "error");
    };

    return () => {
      source.close();
    };
  }, []);

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
        <span
          className={`inline-block h-1.5 w-1.5 rounded-full ${
            state === "open" ? "bg-emerald-500" : state === "connecting" ? "bg-amber-500" : "bg-slate-400"
          }`}
        />
        {state === "open" && "live"}
        {state === "connecting" && "connecting…"}
        {state === "unavailable" && "alert stream not configured for this deployment"}
        {state === "error" && "reconnecting…"}
      </div>

      {alerts.length === 0 ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">No alerts yet this session.</p>
      ) : (
        <ul className="max-h-56 space-y-1 overflow-y-auto text-xs">
          {alerts.map((a) => (
            <li
              key={a.id}
              className="rounded border border-amber-200 bg-amber-50 px-2 py-1 dark:border-amber-900 dark:bg-amber-950/40"
            >
              <div className="font-medium text-slate-800 dark:text-slate-100">
                {summarize(a.raw)}
              </div>
              <div className="text-slate-500 dark:text-slate-400">
                {new Date(a.receivedAt).toLocaleTimeString()}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// The Alert proto's exact field set isn't pinned in this client — it's read
// defensively so a schema addition doesn't break rendering, only the
// specificity of the summary line.
function summarize(raw: Record<string, unknown>): string {
  const detection = (raw.detection ?? {}) as Record<string, unknown>;
  const plate =
    (detection.normalised_text as string) ??
    (detection.raw_text as string) ??
    (raw.plate as string);
  const camera = (detection.camera_id as string) ?? (raw.camera_id as string);
  if (plate && camera) return `${plate} at ${camera}`;
  if (plate) return plate;
  if (camera) return `activity at ${camera}`;
  return "alert";
}
