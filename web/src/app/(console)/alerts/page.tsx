// Deliberately a stub: the BFF relays live alerts over SSE but has no
// persistence/query endpoint for history, so an honest placeholder beats a
// page that can only ever be empty. Live alerts are the console rail.
export default function AlertsPage() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm dark:border-slate-800 dark:bg-slate-900">
        <h1 className="mb-1 text-sm font-semibold text-slate-900 dark:text-slate-50">
          Alert history
        </h1>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          Alert history requires backend persistence; live alerts are on the console rail.
        </p>
      </div>
    </div>
  );
}
