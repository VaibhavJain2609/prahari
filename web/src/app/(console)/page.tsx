"use client";

import { Suspense, useCallback, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import CameraMap, { FlyTo } from "@/components/CameraMap";
import CameraDrawer from "@/components/CameraDrawer";
import SummaryStrip from "@/components/SummaryStrip";
import TraceDock from "@/components/TraceDock";
import AlertsRail from "@/components/AlertsRail";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import { RouteResult } from "@/lib/api";

// useSearchParams() suspends during prerender — the console has to sit
// under a Suspense boundary or `next build` fails the page.
export default function ConsolePage() {
  return (
    <Suspense fallback={null}>
      <OpsConsole />
    </Suspense>
  );
}

// The ops console: map + alert rail + trace dock + camera drawer. Selection
// state lives in the URL (`?camera=`, `?trace=`) so a view is a shareable
// link; everything else (the route result, fly-to requests) is component
// state because it is transient output, not navigation.
function OpsConsole() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [route, setRoute] = useState<RouteResult | null>(null);
  const [flyTo, setFlyTo] = useState<FlyTo | null>(null);
  const flySeq = useRef(0);
  const [showDarkZones, setShowDarkZones] = useState(false);

  const cameraId = searchParams.get("camera");
  const tracePlate = searchParams.get("trace");

  // router.replace, not push: selecting a camera or consuming a trace param
  // shouldn't pile entries onto the back stack.
  const mutateParams = useCallback(
    (mutate: (qs: URLSearchParams) => void) => {
      const qs = new URLSearchParams(searchParams.toString());
      mutate(qs);
      const s = qs.toString();
      router.replace(s ? `${pathname}?${s}` : pathname);
    },
    [router, pathname, searchParams],
  );

  const openCamera = useCallback(
    (id: string) => mutateParams((qs) => qs.set("camera", id)),
    [mutateParams],
  );
  const closeCamera = useCallback(
    () => mutateParams((qs) => qs.delete("camera")),
    [mutateParams],
  );
  const consumeTrace = useCallback(
    () => mutateParams((qs) => qs.delete("trace")),
    [mutateParams],
  );
  const requestFlyTo = useCallback(
    (to: Omit<FlyTo, "seq">) => setFlyTo({ ...to, seq: ++flySeq.current }),
    [],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <SummaryStrip />
      <div className="flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          <CameraMap
            trace={route}
            onCameraSelect={openCamera}
            flyTo={flyTo}
            showDarkZones={showDarkZones}
            onToggleDarkZones={setShowDarkZones}
          />
          <PanelErrorBoundary title="Plate trace">
            <TraceDock
              route={route}
              onRoute={setRoute}
              tracePlate={tracePlate}
              onConsumeTrace={consumeTrace}
              onFlyTo={requestFlyTo}
              onCameraSelect={openCamera}
            />
          </PanelErrorBoundary>
          {cameraId && (
            <PanelErrorBoundary title="Camera detail">
              <CameraDrawer cameraId={cameraId} onClose={closeCamera} />
            </PanelErrorBoundary>
          )}
        </div>
        <aside className="w-80 shrink-0 border-l border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-950">
          <PanelErrorBoundary title="Alerts">
            <AlertsRail onCameraSelect={openCamera} />
          </PanelErrorBoundary>
        </aside>
      </div>
    </div>
  );
}
