"use client";

import { Suspense, useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import AlertsTable from "@/components/alerts/AlertsTable";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import { AlertFilters } from "@/lib/alert-history";

// useSearchParams() suspends during prerender — same Suspense wrapping as
// the console and cameras pages.
export default function AlertsPage() {
  return (
    <Suspense fallback={null}>
      <AlertsView />
    </Suspense>
  );
}

// Alert history: the match engine's persisted store plus the live stream,
// behind a filter bar mirrored into the URL so a filtered view is a
// shareable link — the same convention as the cameras page. `?camera=` on
// this page is the camera filter (on the console page it opens the map
// drawer instead; the table links there).
function AlertsView() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const ackParam = searchParams.get("ack");
  const filters: AlertFilters = {
    plate: searchParams.get("plate") ?? "",
    camera: searchParams.get("camera") ?? "",
    since: searchParams.get("since") ?? "",
    ack: ackParam === "acked" || ackParam === "unacked" ? ackParam : "all",
  };

  const onFilters = useCallback(
    (next: Partial<AlertFilters>) => {
      const qs = new URLSearchParams(searchParams.toString());
      for (const [key, value] of Object.entries(next)) {
        const v = String(value);
        if (v === "" || v === "all") qs.delete(key);
        else qs.set(key, v);
      }
      const s = qs.toString();
      // replace, not push — same convention as the console page: a filter
      // tweak shouldn't pile entries onto the back stack.
      router.replace(s ? `${pathname}?${s}` : pathname);
    },
    [router, pathname, searchParams],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-4 py-2">
        <h1 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
          Alerts
        </h1>
      </div>
      <PanelErrorBoundary title="Alerts">
        <AlertsTable filters={filters} onFilters={onFilters} />
      </PanelErrorBoundary>
    </div>
  );
}
