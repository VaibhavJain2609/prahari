"use client";

import OrgSection from "@/components/admin/OrgSection";
import UsersSection from "@/components/admin/UsersSection";
import ApiKeysSection from "@/components/admin/ApiKeysSection";
import AuditViewer from "@/components/admin/AuditViewer";
import WatchlistSection from "@/components/admin/WatchlistSection";
import CatalogueSection from "@/components/admin/CatalogueSection";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";
import { usePrincipal } from "@/lib/principal";
import { can } from "@/lib/rbac";

// Admin board. The gate is honest both ways: a non-admin sees a plain
// "role required" state with no forms rendered (a form a principal can't
// use is a promise the UI can't keep), and enforcement stays server-side —
// this check only decides what to draw.
export default function AdminPage() {
  const { principal, orgs } = usePrincipal();

  if (!can(principal, "admin")) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <h1 className="mb-1 text-sm font-semibold text-slate-900 dark:text-slate-50">
            Admin role required
          </h1>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            Signed in as {principal.subject} · {principal.role}. Ask an admin for access.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto grid w-full max-w-4xl flex-1 grid-cols-1 gap-4 overflow-y-auto p-4 md:grid-cols-2">
      <PanelErrorBoundary title="Organization">
        <OrgSection orgs={orgs} />
      </PanelErrorBoundary>
      <PanelErrorBoundary title="Users">
        <UsersSection />
      </PanelErrorBoundary>
      <PanelErrorBoundary title="API keys">
        <ApiKeysSection />
      </PanelErrorBoundary>
      <PanelErrorBoundary title="Audit log">
        <AuditViewer />
      </PanelErrorBoundary>
      <PanelErrorBoundary title="Watchlist">
        <WatchlistSection />
      </PanelErrorBoundary>
      <PanelErrorBoundary title="Catalogue">
        <CatalogueSection />
      </PanelErrorBoundary>
    </div>
  );
}
