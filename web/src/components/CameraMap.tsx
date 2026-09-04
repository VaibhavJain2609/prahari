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
import { HEALTH_COLORS, HEALTH_LABELS, HealthState } from "@/lib/health";

// Gujarat's approximate centroid - a reasonable default before any camera
// data has loaded, not a claim about where cameras actually are.
const DEFAULT_CENTER: [number, number] = [71.1924, 22.2587];
const DEFAULT_ZOOM = 6.5;
const POLL_INTERVAL_MS = 20_000;
const SOURCE_ID = "cameras";
const LAYER_ID = "camera-points";

const MAP_STYLE_URL =
  process.env.NEXT_PUBLIC_MAP_STYLE_URL ?? "https://demotiles.maplibre.org/style.json";

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
  const [status, setStatus] = useState<FetchState>("loading");
  const [cameraCount, setCameraCount] = useState<number | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new MapLibreMap({
      container: containerRef.current,
      style: MAP_STYLE_URL,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    map.addControl(new NavigationControl(), "top-right");
    mapRef.current = map;

    // "style.load" fires once the style spec is parsed and ready to accept
    // custom sources/layers. "load" waits for every tile of every basemap
    // layer to finish too, which can lag well behind — our markers don't
    // need to wait on that.
    map.on("style.load", () => {
      map.addSource(SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });

      map.addLayer({
        id: LAYER_ID,
        type: "circle",
        source: SOURCE_ID,
        paint: {
          "circle-radius": 6,
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#0f172a",
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

      map.on("mouseenter", LAYER_ID, () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", LAYER_ID, () => {
        map.getCanvas().style.cursor = "";
      });

      map.on("click", LAYER_ID, (e: MapLayerMouseEvent) => {
        const feature = e.features?.[0];
        if (!feature || feature.geometry.type !== "Point") return;
        const props = feature.properties as CameraProperties;
        const coords = feature.geometry.coordinates.slice() as [number, number];

        popupRef.current?.remove();
        popupRef.current = new Popup({ closeButton: true })
          .setLngLat(coords)
          .setHTML(
            `<div style="font: 13px system-ui; min-width: 180px;">
              <strong>${escapeHtml(props.site_name)}</strong><br/>
              <span style="color:${HEALTH_COLORS[props.state] ?? HEALTH_COLORS.unknown}">
                ${HEALTH_LABELS[props.state] ?? "Unknown"}
              </span>
              ${props.reason ? ` &mdash; ${escapeHtml(props.reason)}` : ""}<br/>
              ${escapeHtml(props.district)} &middot; ${escapeHtml(props.department)}<br/>
              ${props.observed_fps != null ? `${props.observed_fps.toFixed(1)} fps<br/>` : ""}
              ${props.last_frame_at ? `last frame ${new Date(props.last_frame_at).toLocaleTimeString()}` : "no frames observed"}
            </div>`,
          )
          .addTo(map);
      });
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const res = await fetch("/api/bff/cameras/geojson", { cache: "no-store" });
        if (!res.ok) throw new Error(`status ${res.status}`);
        const geojson = await res.json();
        if (cancelled) return;

        const map = mapRef.current;
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
              ? `${cameraCount}`
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

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
