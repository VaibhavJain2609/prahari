"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, Org, Principal } from "@/lib/api";
import { PrincipalProvider } from "@/lib/principal";
import { PurposeProvider } from "@/lib/purpose";
import AlertStreamProvider from "@/components/AlertStreamProvider";
import ConsoleHeader from "@/components/ConsoleHeader";

// The console shell. Fetches the signed-in principal (and the scoped org
// list) exactly once per console load — every page and panel below reads
// the context instead of re-calling auth/me. A 401 never lands here: the
// global handler in api.ts already redirects to /login, so an error that
// reaches this component is a real backend failure worth a retry button.
export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "error"; message: string }
    | { status: "ready"; principal: Principal; org: Org | null; orgs: Org[] }
  >({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await api.me();
        const orgs = await api.listOrgs();
        if (cancelled) return;
        setState({
          status: "ready",
          principal: me,
          org: orgs.find((o) => o.id === me.org_id) ?? null,
          orgs,
        });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          // The global redirect usually already fired; this is the belt to
          // its suspenders for the race where it didn't.
          router.replace("/login");
          return;
        }
        setState({ status: "error", message: "Could not reach the console backend." });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router, attempt]);

  if (state.status === "loading") {
    return (
      <div className="flex flex-1 items-center justify-center">
        <span
          role="status"
          aria-label="Loading console"
          className="h-6 w-6 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600 dark:border-slate-700 dark:border-t-slate-300"
        />
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <p className="mb-4 text-xs text-slate-500 dark:text-slate-400">{state.message}</p>
          <button
            onClick={() => {
              setState({ status: "loading" });
              setAttempt((a) => a + 1);
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <PrincipalProvider
      value={{ principal: state.principal, org: state.org, orgs: state.orgs }}
    >
      <PurposeProvider>
        <AlertStreamProvider>
          <div className="flex min-h-0 flex-1 flex-col">
            <ConsoleHeader />
            <div className="flex min-h-0 flex-1 flex-col">{children}</div>
          </div>
        </AlertStreamProvider>
      </PurposeProvider>
    </PrincipalProvider>
  );
}
