"use client";

import { Suspense, useCallback, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import CamerasTable, { CameraFilters } from "@/components/cameras/CamerasTable";
import CameraRegisterForm from "@/components/cameras/CameraRegisterForm";
import CsvImport from "@/components/cameras/CsvImport";
import CameraDrawer from "@/components/CameraDrawer";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import { usePrincipal } from "@/lib/principal";
import { can } from "@/lib/rbac";

export default function CamerasPage() {
  return (
    <Suspense fallback={null}>
      <CamerasView />
    </Suspense>
  );
}

function CamerasView() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { principal } = usePrincipal();
  const [modal, setModal] = useState<"register" | "import" | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  // Filters are read straight off the URL; onFilters writes them back.
  const filters: CameraFilters = {
    district: searchParams.get("district") ?? "",
    department: searchParams.get("department") ?? "",
    state: searchParams.get("state") ?? "",
    lifecycle: searchParams.get("lifecycle") ?? "active",
    search: searchParams.get("search") ?? "",
    offset: Number(searchParams.get("offset") ?? 0) || 0,
  };
  const cameraId = searchParams.get("camera");

  const mutateParams = useCallback(
    (mutate: (qs: URLSearchParams) => void) => {
      const qs = new URLSearchParams(searchParams.toString());
      mutate(qs);
      const s = qs.toString();
      router.replace(s ? `${pathname}?${s}` : pathname);
    },
    [router, pathname, searchParams],
  );

  const onFilters = useCallback(
    (next: Partial<CameraFilters>) =>
      mutateParams((qs) => {
        for (const [key, value] of Object.entries(next)) {
          const v = String(value);
          if (v === "" || v === "0") qs.delete(key);
          else qs.set(key, v);
        }
      }),
    [mutateParams],
  );

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-4 py-2">
        <h1 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Cameras</h1>
        {can(principal, "operator") && (
          <div className="flex gap-2">
            <button
              onClick={() => setModal("register")}
              className="rounded bg-slate-900 px-2 py-1 text-xs font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
            >
              Register camera
            </button>
            <button
              onClick={() => setModal("import")}
              className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Import CSV
            </button>
          </div>
        )}
      </div>

      <CamerasTable
        filters={filters}
        onFilters={onFilters}
        onSelect={(id) => mutateParams((qs) => qs.set("camera", id))}
        reloadTick={reloadTick}
      />

      {cameraId && (
        <PanelErrorBoundary title="Camera detail">
          <CameraDrawer
            cameraId={cameraId}
            onClose={() => mutateParams((qs) => qs.delete("camera"))}
            onChanged={() => setReloadTick((t) => t + 1)}
          />
        </PanelErrorBoundary>
      )}

      {modal && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4"
          onClick={() => setModal(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={modal === "register" ? "Register camera" : "Import CSV"}
            className="w-full max-w-lg rounded-lg border border-slate-200 bg-white p-4 shadow-xl dark:border-slate-800 dark:bg-slate-900"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                {modal === "register" ? "Register camera" : "Import CSV"}
              </h2>
              <button
                onClick={() => setModal(null)}
                aria-label="Close"
                className="rounded px-1.5 py-0.5 text-xs text-slate-500 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-400 dark:hover:bg-slate-800"
              >
                ✕
              </button>
            </div>
            {modal === "register" ? (
              <CameraRegisterForm
                onRegistered={() => {
                  setModal(null);
                  setReloadTick((t) => t + 1);
                }}
              />
            ) : (
              <CsvImport onImported={() => setReloadTick((t) => t + 1)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
