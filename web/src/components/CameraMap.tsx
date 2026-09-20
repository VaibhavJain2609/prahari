"use client";

import { useEffect, useRef, useState } from "react";
import {
  GeoJSONSource,
  Map as MapLibreMap,
  MapLayerMouseEvent,
  NavigationControl,
  Popup,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { api, CameraGeoJSON } from "@/lib/api";
import { HEALTH_COLORS, HEALTH_LABELS, HealthState } from "@/lib/health";
import { getStoredTheme, useTheme } from "@/lib/theme";

// Gujarat's approximate centroid - a reasonable default before any camera
// data has loaded, not a claim about where cameras actually are.
const DEFAULT_CENTER: [number, number] = [71.1924, 22.2587];
const DEFAULT_ZOOM = 6.5;
const POLL_INTERVAL_MS = 20_000;
const BBOX_DEBOUNCE_MS = 350;
const SOURCE_ID = "cameras";
const CLUSTER_LAYER_ID = "camera-clusters";
const CLUSTER_COUNT_LAYER_ID = "camera-cluster-count";
const LAYER_ID = "camera-points";

// The registry caps geojson `limit` at 100_000; ask for the ceiling and,
// when a FeatureCollection comes back exactly that long, render the count
// with a ≥ caveat — the backend gives no total, so length-at-limit is the
// only honest signal that truncation happened.
const FETCH_LIMIT = 100_000;

const MAP_STYLE_URL =
  process.env.NEXT_PUBLIC_MAP_STYLE_URL ?? "https://demotiles.maplibre.org/style.json";
const MAP_STYLE_DARK_URL =
  process.env.NEXT_PUBLIC_MAP_STYLE_DARK_URL ??
  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// Read the live theme off <html>, not the React state — the style.load
// handler below closes over its mount-time render, and it re-runs on every
// setStyle long after that render is stale.
function isDarkNow(): boolean {
  return document.documentElement.classList.contains("dark");
}

type CameraProperties = {
  id: string;
  site_name: string;
  district: string;
  department: string;
  state: HealthState;
  reason: string | null;
  observed_fps: number | null;
  last_frame_at: string | null;
};

type FetchState = "loading" | "ok" | "error";

export default function CameraMap() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  const lastGeoJSON = useRef<CameraGeoJSON | null>(null);
  const appliedStyle = useRef<string>(getStoredTheme() === "dark" ? MAP_STYLE_DARK_URL : MAP_STYLE_URL);
  const bboxTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRef = useRef<() => void>(() => {});
  const [status, setStatus] = useState<FetchState>("loading");
  const [cameraCount, setCameraCount] = useState<number | null>(null);
  const { theme } = useTheme();

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new MapLibreMap({
      container: containerRef.current,
      style: appliedStyle.current,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    map.addControl(new NavigationControl(), "top-right");
    mapRef.current = map;

    // "style.load" fires once the style spec is parsed and ready to accept
    // custom sources/layers — and fires again after every setStyle() (theme
    // switch), which is why all of this setup lives inside the handler:
    // a style reload drops every runtime source and layer, so they must be
    // re-added, then the last fetched FeatureCollection re-applied. "load"
    // waits for every basemap tile too, which can lag well behind — our
    // markers don't need to wait on that.
    map.on("style.load", () => {
      if (!map.getSource(SOURCE_ID)) {
        map.addSource(SOURCE_ID, {
          type: "geojson",
          data: { type: "FeatureCollection", features: [] },
          cluster: true,
          clusterMaxZoom: 14,
          clusterRadius: 50,
        });
      }

      if (!map.getLayer(CLUSTER_LAYER_ID)) {
        map.addLayer({
          id: CLUSTER_LAYER_ID,
          type: "circle",
          source: SOURCE_ID,
          filter: ["has", "point_count"],
          paint: {
            "circle-radius": ["step", ["get", "point_count"], 14, 100, 18, 1000, 24],
            "circle-color": ["step", ["get", "point_count"], "#64748b", 100, "#475569", 1000, "#334155"],
            "circle-opacity": 0.75,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": isDarkNow() ? "#e2e8f0" : "#0f172a",
          },
        });
      }

      if (!map.getLayer(CLUSTER_COUNT_LAYER_ID)) {
        map.addLayer({
          id: CLUSTER_COUNT_LAYER_ID,
          type: "symbol",
          source: SOURCE_ID,
          filter: ["has", "point_count"],
          layout: {
            "text-field": ["get", "point_count_abbreviated"],
            "text-size": 12,
          },
          paint: {
            "text-color": "#f8fafc",
          },
        });
      }

      if (!map.getLayer(LAYER_ID)) {
        map.addLayer({
          id: LAYER_ID,
          type: "circle",
          source: SOURCE_ID,
          filter: ["!", ["has", "point_count"]],
          paint: {
            "circle-radius": 6,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": isDarkNow() ? "#e2e8f0" : "#0f172a",
            "circle-color": [
              "match",
              ["get", "state"],
              "healthy",
              HEALTH_COLORS.healthy,
              "degraded",
              HEALTH_COLORS.degraded,
              "unreachable",
              HEALTH_COLORS.unreachable,
              "tampered",
              HEALTH_COLORS.tampered,
              HEALTH_COLORS.unknown,
            ],
          },
        });
      }

      // A style reload just recreated the source empty — put back whatever
      // the last fetch gave us, then let the normal refresh path fill in.
      if (lastGeoJSON.current) {
        (map.getSource(SOURCE_ID) as GeoJSONSource).setData(lastGeoJSON.current);
      }
    });

    map.on("mouseenter", LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
    });
    map.on("mouseenter", CLUSTER_LAYER_ID, () => {
      map.getCanvas().style.cursor = "pointer";
    });
    map.on("mouseleave", CLUSTER_LAYER_ID, () => {
      map.getCanvas().style.cursor = "";
    });

    map.on("click", CLUSTER_LAYER_ID, async (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature || feature.geometry.type !== "Point") return;
      const clusterId = feature.properties?.cluster_id;
      const source = map.getSource(SOURCE_ID) as GeoJSONSource;
      const zoom = await source.getClusterExpansionZoom(clusterId);
      map.easeTo({
        center: feature.geometry.coordinates as [number, number],
        zoom,
      });
    });

    map.on("click", LAYER_ID, (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature || feature.geometry.type !== "Point") return;
      const props = feature.properties as CameraProperties;
      const coords = feature.geometry.coordinates.slice() as [number, number];

      popupRef.current?.remove();
      popupRef.current = new Popup({ closeButton: true, maxWidth: "280px" })
        .setLngLat(coords)
        .setDOMContent(buildPopupContent(props))
        .addTo(map);
    });

    // Viewport-scoped fetch: moving or zooming the map re-queries with the
    // visible bounds so a district-level operator never waits on the whole
    // state. Debounced — a drag produces a burst of moveend-adjacent events.
    map.on("moveend", () => {
      if (bboxTimer.current) clearTimeout(bboxTimer.current);
      bboxTimer.current = setTimeout(() => refreshRef.current(), BBOX_DEBOUNCE_MS);
    });

    return () => {
      if (bboxTimer.current) clearTimeout(bboxTimer.current);
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Theme → basemap. setStyle triggers style.load, which re-runs the
  // source/layer setup above; appliedStyle guards against redundant reloads.
  useEffect(() => {
    const map = mapRef.current;
    const url = theme === "dark" ? MAP_STYLE_DARK_URL : MAP_STYLE_URL;
    if (map && url !== appliedStyle.current) {
      appliedStyle.current = url;
      map.setStyle(url);
    }
  }, [theme]);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      const map = mapRef.current;
      let bbox: string | undefined;
      if (map) {
        const b = map.getBounds();
        bbox = `${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}`;
      }
      try {
        const geojson = await api.getCamerasGeoJSON({ bbox, limit: FETCH_LIMIT });
        if (cancelled) return;
        lastGeoJSON.current = geojson;

        const source = map?.getSource(SOURCE_ID) as GeoJSONSource | undefined;
        if (source) {
          source.setData(geojson);
        } else if (map) {
          // Source isn't ready yet (style still parsing) — apply this
          // fetch's data as soon as it is, instead of dropping it and
          // waiting for the next 20s tick.
          map.once("style.load", () => {
            (map.getSource(SOURCE_ID) as GeoJSONSource | undefined)?.setData(geojson);
          });
        }
        setCameraCount(geojson.features?.length ?? 0);
        setStatus("ok");
      } catch {
        if (!cancelled) setStatus("error");
      }
    }

    refreshRef.current = poll;
    const initial = setTimeout(poll, 300);
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearTimeout(initial);
      clearInterval(interval);
    };
  }, []);

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" />
      <Legend cameraCount={cameraCount} status={status} />
    </div>
  );
}

function Legend({ cameraCount, status }: { cameraCount: number | null; status: FetchState }) {
  return (
    <div className="absolute bottom-4 left-4 rounded-lg bg-white/95 px-3 py-2 text-xs shadow-md dark:bg-slate-900/95">
      <div className="mb-1.5 flex items-center justify-between gap-4 font-medium text-slate-700 dark:text-slate-200">
        <span>Cameras</span>
        <span>
          {status === "error"
            ? "registry unreachable"
            : cameraCount != null
              ? // The FeatureCollection carries no total — at the fetch cap
                // the count is a floor, not the real number.
                `${cameraCount >= FETCH_LIMIT ? "≥" : ""}${cameraCount}`
              : "loading…"}
        </span>
      </div>
      <ul className="space-y-1">
        {(Object.keys(HEALTH_LABELS) as (keyof typeof HEALTH_LABELS)[]).map((state) => (
          <li key={state} className="flex items-center gap-2 text-slate-600 dark:text-slate-300">
            <span
              className="inline-block h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: HEALTH_COLORS[state] }}
            />
            {HEALTH_LABELS[state]}
          </li>
        ))}
      </ul>
    </div>
  );
}

// The popup is DOM, not React — MapLibre renders it outside the React tree.
// The camera-detail read is an audited call (BFF requires X-Purpose-Code),
// so the popup asks for a case reference before issuing it; a click alone
// never touches the audited endpoint.
function buildPopupContent(props: CameraProperties): HTMLElement {
  const root = document.createElement("div");
  root.style.cssText = "font: 13px system-ui; min-width: 200px;";

  const info = document.createElement("div");
  info.innerHTML =
    `<strong>${escapeHtml(props.site_name)}</strong><br/>` +
    `<span style="color:${HEALTH_COLORS[props.state] ?? HEALTH_COLORS.unknown}">` +
    `${HEALTH_LABELS[props.state] ?? "Unknown"}</span>` +
    `${props.reason ? ` &mdash; ${escapeHtml(props.reason)}` : ""}<br/>` +
    `${escapeHtml(props.district)} &middot; ${escapeHtml(props.department)}<br/>` +
    `${props.observed_fps != null ? `${props.observed_fps.toFixed(1)} fps<br/>` : ""}` +
    `${props.last_frame_at ? `last frame ${new Date(props.last_frame_at).toLocaleTimeString()}` : "no frames observed"}`;
  root.appendChild(info);

  const form = document.createElement("form");
  form.style.cssText = "margin-top: 6px; display: flex; gap: 4px;";

  const input = document.createElement("input");
  input.placeholder = "case ref (required)";
  input.setAttribute("aria-label", "Case reference for camera detail lookup");
  input.style.cssText =
    "min-width: 0; flex: 1; border: 1px solid #94a3b8; border-radius: 4px; padding: 2px 6px; font-size: 11px;";

  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "Details";
  button.style.cssText =
    "border: 1px solid #94a3b8; border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer;";

  const result = document.createElement("div");
  result.style.cssText = "margin-top: 4px; font-size: 11px; color: #475569;";
  result.setAttribute("aria-live", "polite");

  form.appendChild(input);
  form.appendChild(button);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const ref = input.value.trim();
    if (!ref) {
      result.textContent = "Enter a case reference first.";
      input.focus();
      return;
    }
    button.disabled = true;
    result.textContent = "loading…";
    try {
      const detail = await api.getCamera(props.id, ref);
      result.textContent = summarizeDetail(detail);
    } catch (err) {
      result.textContent = err instanceof Error ? err.message : "detail lookup failed";
    } finally {
      button.disabled = false;
    }
  });

  root.appendChild(form);
  root.appendChild(result);
  return root;
}

function summarizeDetail(detail: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const key of ["external_id", "site_name", "district", "camera_type", "status"]) {
    const value = detail[key];
    if (value != null && value !== "") parts.push(`${key}: ${String(value)}`);
  }
  return parts.length ? parts.join(" · ") : "detail loaded";
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
