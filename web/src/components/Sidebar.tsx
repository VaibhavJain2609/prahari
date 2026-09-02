// Alert console and plate trace both read from services/bff, which does not
// exist yet (docs/DAY3-DESIGN.md §4). These are placeholders that say so
// plainly rather than faking data - CLAUDE.md is explicit that anything
// that looks like a working feature in a demo has to actually be one.
export default function Sidebar() {
  return (
    <aside className="flex w-80 shrink-0 flex-col gap-4 overflow-y-auto border-l border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-950">
      <Panel title="Plate trace">
        <PendingBff detail="GET /api/v1/routes/{plate} via BFF" />
      </Panel>
      <Panel title="Alert console">
        <PendingBff detail="SSE relay off prahari:alerts via BFF" />
      </Panel>
    </aside>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <h2 className="mb-2 text-sm font-semibold text-slate-800 dark:text-slate-100">{title}</h2>
      {children}
    </section>
  );
}

function PendingBff({ detail }: { detail: string }) {
  return (
    <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
      Waiting on <code className="rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800">services/bff</code> —
      not built yet. Will call <code className="rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-800">{detail}</code>.
    </p>
  );
}
