"use client";

import { useEffect, useRef, useState } from "react";
import {
  GeoJSONSource,
  LngLatBounds,
  Map as MapLibreMap,
  MapLayerMouseEvent,
  NavigationControl,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { api, CameraGeoJSON, DarkZoneInfo, RouteResult } from "@/lib/api";
import { HEALTH_COLORS, HEALTH_LABELS } from "@/lib/health";
import { getStoredTheme, useTheme } from "@/lib/theme";
import { useBFF } from "@/lib/use-bff";

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

// Trace overlay (all re-added on style.load — a basemap swap drops every
// runtime source and layer).
const ROUTE_SOURCE_ID = "trace-route";
const ROUTE_CASING_LAYER_ID = "trace-link-casing";
const ROUTE_PLATE_LAYER_ID = "trace-links-plate";
const ROUTE_BRIDGED_LAYER_ID = "trace-links-bridged";
const HOP_SOURCE_ID = "trace-hops";
const HOP_LAYER_ID = "trace-hop-points";
const HOP_LABEL_LAYER_ID = "trace-hop-labels";
const REJECTED_SOURCE_ID = "trace-rejected";
const REJECTED_LAYER_ID = "trace-rejected-points";
const DARK_ZONE_SOURCE_ID = "dark-zones";
const DARK_ZONE_LAYER_ID = "dark-zone-points";

// Route styling. Plate links and bridged links are separate layers because
// line-dasharray is not data-driven in MapLibre — a filter split is the only
// honest way to dash just the bridged segments.
const PLATE_LINK_COLOR = "#38bdf8"; // sky-400
const BRIDGED_LINK_COLOR = "#f59e0b"; // amber-500
const REJECTED_COLOR = "#f59e0b"; // amber-500
const DARK_ZONE_COLOR = "#f43f5e"; // rose-500

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

type FetchState = "loading" | "ok" | "error";

type FeatureCollection = {
  type: "FeatureCollection";
  features: Record<string, unknown>[];
};

const EMPTY_FC: FeatureCollection = { type: "FeatureCollection", features: [] };

// A fly-to request from outside the map (a trace hop click, a table row).
// `seq` is a monotonically increasing counter so re-requesting the same
// coordinates still triggers a fly — an object identity check would too,
// but the seq makes the intent explicit at the call site.
export type FlyTo = { latitude: number; longitude: number; zoom?: number; seq: number };

type Props = {
  // The route to overlay, or null to clear it. Detail (which hop, which
  // camera) is the dock/drawer's job — the map draws geometry only.
  trace?: RouteResult | null;
  // Replaces the old in-map popup: the drawer is the detail surface, and a
  // click is just "select this camera id".
  onCameraSelect?: (cameraId: string) => void;
  flyTo?: FlyTo | null;
  // When on, fetch gaps/dark-zones and render rose rings — an operator's
  // "where are the blind spots" toggle, distinct from the dark zones a
  // route response happens to carry.
  showDarkZones?: boolean;
  onToggleDarkZones?: (show: boolean) => void;
};

export default function CameraMap({
  trace = null,
  onCameraSelect,
  flyTo = null,
  showDarkZones = false,
  onToggleDarkZones,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const lastGeoJSON = useRef<CameraGeoJSON | null>(null);
  // camera_id → [lon, lat] from the last geojson fetch — used to resolve the
  // rejected hops' camera ids (they carry no locations of their own).
  const locationsById = useRef<Map<string, [number, number]>>(new Map());
  // Last computed overlay data, so a style reload can re-apply it.
  const overlays = useRef<{
    links: FeatureCollection;
    hops: FeatureCollection;
    rejected: FeatureCollection;
    darkZones: FeatureCollection;
  }>({ links: EMPTY_FC, hops: EMPTY_FC, rejected: EMPTY_FC, darkZones: EMPTY_FC });
  const appliedStyle = useRef<string>(getStoredTheme() === "dark" ? MAP_STYLE_DARK_URL : MAP_STYLE_URL);
  const bboxTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRef = useRef<() => void>(() => {});
  const onCameraSelectRef = useRef(onCameraSelect);
  const lastFlySeq = useRef<number | null>(null);
  const [status, setStatus] = useState<FetchState>("loading");
  const [cameraCount, setCameraCount] = useState<number | null>(null);
  const { theme } = useTheme();

  // Dark-zone overlay: fetched on demand (the toggle), merged with whatever
  // dark zones the route response carried.
  const { data: fetchedDarkZones } = useBFF<DarkZoneInfo[]>(
    showDarkZones ? "gaps/dark-zones" : null,
  );

  // Keep the latest select callback in a ref — the map's click handlers are
  // registered once at mount and would otherwise close over a stale prop.
  useEffect(() => {
    onCameraSelectRef.current = onCameraSelect;
  }, [onCameraSelect]);

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

      // --- trace overlays -------------------------------------------------
      // Order matters: casing under the links, links under the hop markers.
      for (const id of [ROUTE_SOURCE_ID, HOP_SOURCE_ID, REJECTED_SOURCE_ID, DARK_ZONE_SOURCE_ID]) {
        if (!map.getSource(id)) {
          map.addSource(id, { type: "geojson", data: EMPTY_FC as never });
        }
      }

      if (!map.getLayer(ROUTE_CASING_LAYER_ID)) {
        map.addLayer({
          id: ROUTE_CASING_LAYER_ID,
          type: "line",
          source: ROUTE_SOURCE_ID,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-width": 6,
            "line-color": isDarkNow() ? "#0f172a" : "#ffffff",
            "line-opacity": 0.6,
          },
        });
      }
      if (!map.getLayer(ROUTE_PLATE_LAYER_ID)) {
        map.addLayer({
          id: ROUTE_PLATE_LAYER_ID,
          type: "line",
          source: ROUTE_SOURCE_ID,
          filter: ["!=", ["get", "link_kind"], "bridged"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-width": 2.5, "line-color": PLATE_LINK_COLOR },
        });
      }
      if (!map.getLayer(ROUTE_BRIDGED_LAYER_ID)) {
        map.addLayer({
          id: ROUTE_BRIDGED_LAYER_ID,
          type: "line",
          source: ROUTE_SOURCE_ID,
          filter: ["==", ["get", "link_kind"], "bridged"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-width": 2.5,
            "line-color": BRIDGED_LINK_COLOR,
            "line-dasharray": [2, 1.5],
          },
        });
      }
      if (!map.getLayer(HOP_LAYER_ID)) {
        map.addLayer({
          id: HOP_LAYER_ID,
          type: "circle",
          source: HOP_SOURCE_ID,
          paint: {
            "circle-radius": 9,
            "circle-color": PLATE_LINK_COLOR,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": isDarkNow() ? "#0f172a" : "#ffffff",
          },
        });
      }
      if (!map.getLayer(HOP_LABEL_LAYER_ID)) {
        map.addLayer({
          id: HOP_LABEL_LAYER_ID,
          type: "symbol",
          source: HOP_SOURCE_ID,
          layout: {
            // No explicit text-font — the default stack (Open Sans / Arial
            // Unicode MS) is what both basemap styles actually ship glyphs
            // for; naming a font they don't carry renders nothing.
            "text-field": ["get", "seq"],
            "text-size": 9,
          },
          paint: { "text-color": "#0c4a6e" },
        });
      }
      if (!map.getLayer(REJECTED_LAYER_ID)) {
        map.addLayer({
          id: REJECTED_LAYER_ID,
          type: "circle",
          source: REJECTED_SOURCE_ID,
          paint: {
            "circle-radius": 6,
            "circle-color": REJECTED_COLOR,
            "circle-opacity": 0.8,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": isDarkNow() ? "#0f172a" : "#ffffff",
          },
        });
      }
      if (!map.getLayer(DARK_ZONE_LAYER_ID)) {
        map.addLayer({
          id: DARK_ZONE_LAYER_ID,
          type: "circle",
          source: DARK_ZONE_SOURCE_ID,
          paint: {
            // Ring, not fill: a blind spot should read as "absence", and a
            // solid dot would masquerade as a camera.
            "circle-radius": 11,
            "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 2,
            "circle-stroke-color": DARK_ZONE_COLOR,
          },
        });
      }

      // A style reload just recreated the sources empty — put back whatever
      // the last fetch/derive gave us, then let the normal refresh fill in.
      if (lastGeoJSON.current) {
        (map.getSource(SOURCE_ID) as GeoJSONSource).setData(lastGeoJSON.current);
      }
      applyOverlays(map, overlays.current);
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

    const selectCamera = (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature || feature.geometry.type !== "Point") return;
      // Camera pins carry `id`; trace hop/rejected points carry `camera_id`.
      const props = feature.properties as Record<string, unknown>;
      const id = (props.id ?? props.camera_id) as string | undefined;
      if (id) onCameraSelectRef.current?.(id);
    };
    // Camera pins, trace hops and rejected sightings all resolve to a
    // camera id — the drawer is the detail surface for all of them.
    map.on("click", LAYER_ID, selectCamera);
    map.on("click", HOP_LAYER_ID, selectCamera);
    map.on("click", REJECTED_LAYER_ID, selectCamera);

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
        rebuildLocationIndex(geojson, locationsById.current);

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

  // Trace + dark-zone props → overlay FeatureCollections → map sources.
  useEffect(() => {
    const derived = trace ? deriveTraceFeatures(trace, locationsById.current) : null;
    overlays.current.links = derived?.links ?? EMPTY_FC;
    overlays.current.hops = derived?.hops ?? EMPTY_FC;
    overlays.current.rejected = derived?.rejected ?? EMPTY_FC;
    overlays.current.darkZones = darkZoneFeatures(fetchedDarkZones ?? [], trace);
    const map = mapRef.current;
    applyOverlays(map, overlays.current);
    if (trace && derived?.bounds) {
      // Reveal the whole route in one move — 60px of padding keeps the end
      // hops clear of the dock and the legend.
      map?.fitBounds(derived.bounds, { padding: 60, maxZoom: 15 });
    }
  }, [trace, fetchedDarkZones]);

  // External fly-to requests (a hop click in the dock, a row in the table).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !flyTo || flyTo.seq === lastFlySeq.current) return;
    lastFlySeq.current = flyTo.seq;
    map.flyTo({
      center: [flyTo.longitude, flyTo.latitude],
      zoom: flyTo.zoom ?? Math.max(map.getZoom(), 14),
    });
  }, [flyTo]);

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" />
      <Legend
        cameraCount={cameraCount}
        status={status}
        showDarkZones={showDarkZones}
        onToggleDarkZones={onToggleDarkZones}
      />
    </div>
  );
}

// --- trace → GeoJSON derivation ----------------------------------------------
// (module-level, not component closures: the style.load handler and the
// effects both call these, and hoisted component functions trip the React
// compiler's "accessed before declared" lint.)

type OverlayData = {
  links: FeatureCollection;
  hops: FeatureCollection;
  rejected: FeatureCollection;
  darkZones: FeatureCollection;
};

function applyOverlays(map: MapLibreMap | null, data: OverlayData) {
  if (!map) return;
  const set = (id: string, fc: FeatureCollection) => {
    const source = map.getSource(id) as GeoJSONSource | undefined;
    source?.setData(fc as never);
  };
  set(ROUTE_SOURCE_ID, data.links);
  set(HOP_SOURCE_ID, data.hops);
  set(REJECTED_SOURCE_ID, data.rejected);
  set(DARK_ZONE_SOURCE_ID, data.darkZones);
}

function rebuildLocationIndex(
  geojson: CameraGeoJSON,
  into: Map<string, [number, number]>,
) {
  into.clear();
  for (const f of geojson.features ?? []) {
    const id = f.properties?.id;
    if (typeof id === "string" && f.geometry?.type === "Point") {
      into.set(id, f.geometry.coordinates);
    }
  }
}

type TraceFeatures = {
  links: FeatureCollection;
  hops: FeatureCollection;
  rejected: FeatureCollection;
  bounds: LngLatBounds | null;
};

// One LineString per consecutive hop pair where *both* ends have a
// location — a hop the correlation service couldn't place can't be drawn,
// and drawing to a guessed point would invent geography. Each segment takes
// its `link_kind` from the destination hop, matching how the backend
// records the link (the first hop has none — it links from nothing).
function deriveTraceFeatures(
  trace: RouteResult,
  locationsById: Map<string, [number, number]>,
): TraceFeatures {
  const links: Record<string, unknown>[] = [];
  const hops: Record<string, unknown>[] = [];
  const rejected: Record<string, unknown>[] = [];
  const bounds = new LngLatBounds();
  let anyBound = false;

  const located = trace.hops.map((hop, i) => ({
    hop,
    seq: i + 1,
    coord: hop.location
      ? ([hop.location.longitude, hop.location.latitude] as [number, number])
      : null,
  }));

  for (const { hop, seq, coord } of located) {
    if (!coord) continue;
    hops.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: coord },
      properties: { seq: String(seq), camera_id: hop.camera_id },
    });
    bounds.extend(coord);
    anyBound = true;
  }

  for (let i = 1; i < located.length; i++) {
    const prev = located[i - 1];
    const cur = located[i];
    if (!prev.coord || !cur.coord) continue;
    links.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [prev.coord, cur.coord] },
      properties: { link_kind: cur.hop.link_kind ?? "plate" },
    });
  }

  // Rejected sightings carry only camera ids; resolve them against the
  // camera feed's locations. A camera outside the current viewport fetch
  // (or not in scope) simply doesn't resolve — the dock still lists it.
  for (const r of trace.rejected) {
    for (const cameraId of [r.from_camera_id, r.to_camera_id]) {
      const coord = locationsById.get(cameraId);
      if (!coord) continue;
      rejected.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: coord },
        properties: { camera_id: cameraId, reason: r.reason },
      });
      bounds.extend(coord);
      anyBound = true;
    }
  }

  return {
    links: { type: "FeatureCollection", features: links },
    hops: { type: "FeatureCollection", features: hops },
    rejected: { type: "FeatureCollection", features: rejected },
    bounds: anyBound ? bounds : null,
  };
}

// Dark zones from two sources, same ring: the operator's overlay toggle
// (fetched from gaps/dark-zones, with site/district detail) and whatever the
// route response carried. Deduped on camera_id — the same blind spot shows
// up in both when a trace crosses it.
function darkZoneFeatures(
  fetched: DarkZoneInfo[],
  trace: RouteResult | null,
): FeatureCollection {
  const seen = new Set<string>();
  const features: Record<string, unknown>[] = [];
  const push = (cameraId: string, loc: { latitude: number; longitude: number } | null, extra: Record<string, unknown>) => {
    if (!loc || seen.has(cameraId)) return;
    seen.add(cameraId);
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [loc.longitude, loc.latitude] },
      properties: { camera_id: cameraId, ...extra },
    });
  };
  for (const z of fetched) {
    push(z.camera_id, z.location, {
      site_name: z.site_name,
      district: z.district,
      reason: z.reason,
      nearest_healthy_m: z.nearest_healthy_m,
    });
  }
  for (const z of trace?.dark_zones ?? []) {
    push(z.camera_id, z.location ?? null, {});
  }
  return { type: "FeatureCollection", features };
}

function Legend({
  cameraCount,
  status,
  showDarkZones,
  onToggleDarkZones,
}: {
  cameraCount: number | null;
  status: FetchState;
  showDarkZones: boolean;
  onToggleDarkZones?: (show: boolean) => void;
}) {
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
        {onToggleDarkZones && (
          <li>
            <button
              onClick={() => onToggleDarkZones(!showDarkZones)}
              aria-pressed={showDarkZones}
              className="flex items-center gap-2 text-slate-600 hover:text-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-300 dark:hover:text-slate-100"
            >
              <span
                className="inline-block h-2.5 w-2.5 rounded-full border-2"
                style={{ borderColor: DARK_ZONE_COLOR }}
              />
              Dark zones {showDarkZones ? "on" : "off"}
            </button>
          </li>
        )}
      </ul>
    </div>
  );
}
