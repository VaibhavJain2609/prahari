"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import CameraMap from "@/components/CameraMap";
import Sidebar from "@/components/Sidebar";
import { api, ApiError, Org, Principal } from "@/lib/api";

// One console, three boards — global, organization, local body — chosen
// here by the signed-in principal's own org `kind`, never by a client-side
// role guess. The map and the health legend (CameraMap.tsx) don't change at
// all between boards; only what Sidebar shows below them does.
export default function Home() {
  const router = useRouter();
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [org, setOrg] = useState<Org | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await api.me();
        const orgs = await api.listOrgs();
        if (cancelled) return;
        setPrincipal(me);
        setOrg(orgs.find((o) => o.id === me.org_id) ?? null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
          return;
        }
        setLoadError("Could not reach the console backend.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function onLogout() {
    await api.logout().catch(() => undefined);
    router.replace("/login");
  }

  return (
    <div className="flex flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-baseline gap-3">
          <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">PRAHARI</h1>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {org ? `${boardLabel(org.kind)} — ${org.name}` : "Gujarat Sentinel"}
          </span>
        </div>
        {principal && (
          <div className="flex items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
            <span>
              {principal.subject} · {principal.role}
            </span>
            <button
              onClick={onLogout}
              className="rounded border border-slate-300 px-2 py-1 text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Sign out
            </button>
          </div>
        )}
      </header>
      <div className="flex flex-1 overflow-hidden">
        <main className="flex-1">
          <CameraMap />
        </main>
        {loadError ? (
          <aside className="w-80 shrink-0 border-l border-slate-200 p-4 text-xs text-red-600 dark:border-slate-800 dark:text-red-400">
            {loadError}
          </aside>
        ) : (
          <Sidebar principal={principal} org={org} />
        )}
      </div>
    </div>
  );
}

function boardLabel(kind: Org["kind"]): string {
  if (kind === "state") return "Statewide";
  if (kind === "organization") return "Organization";
  return "Local body";
}
