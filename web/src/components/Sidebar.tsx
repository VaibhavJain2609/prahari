import { Org, Principal } from "@/lib/api";
import PlateTracePanel from "@/components/PlateTracePanel";
import AlertPanel from "@/components/AlertPanel";
import OnboardingPanel from "@/components/OnboardingPanel";
import AdminPanel from "@/components/AdminPanel";
import PanelErrorBoundary from "@/components/PanelErrorBoundary";

// Plate trace and the alert console are the mandatory-path panels — every
// board shows both, identically, regardless of role or org kind (the BFF
// scopes the data, not this component). Onboarding is gated to operator+
// (a viewer can look but not register cameras); org administration is
// gated to admin. Nothing here fakes data behind a role it can't reach —
// a panel a principal can't use simply isn't rendered.
export default function Sidebar({ principal, org }: { principal: Principal | null; org: Org | null }) {
  const canOnboard = principal?.role === "operator" || principal?.role === "admin";
  const canAdminister = principal?.role === "admin";

  return (
    <aside className="flex w-80 shrink-0 flex-col gap-4 overflow-y-auto border-l border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-950">
      <Panel title="Plate trace">
        <PanelErrorBoundary title="Plate trace">
          <PlateTracePanel />
        </PanelErrorBoundary>
      </Panel>
      <Panel title="Alert console">
        <PanelErrorBoundary title="Alert console">
          <AlertPanel />
        </PanelErrorBoundary>
      </Panel>
      {canOnboard && (
        <Panel title="Camera onboarding" caption={org ? `into ${org.name}` : undefined}>
          <PanelErrorBoundary title="Camera onboarding">
            <OnboardingPanel />
          </PanelErrorBoundary>
        </Panel>
      )}
      {canAdminister && principal && (
        <Panel title="Administration" caption={org ? `subtree of ${org.name}` : undefined}>
          <PanelErrorBoundary title="Administration">
            <AdminPanel principal={principal} />
          </PanelErrorBoundary>
        </Panel>
      )}
    </aside>
  );
}

function Panel({
  title,
  caption,
  children,
}: {
  title: string;
  caption?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="mb-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
        {title}
        {caption && <span className="ml-2 text-xs font-normal text-slate-400">{caption}</span>}
      </h2>
      {children}
    </section>
  );
}
