// Route skeleton for the console segment — shown while the (client) layout
// resolves the principal or a page suspends on searchParams.
export default function Loading() {
  return (
    <div className="flex min-h-0 flex-1 animate-pulse flex-col">
      <div className="h-10 border-b border-slate-200 dark:border-slate-800" />
      <div className="flex min-h-0 flex-1">
        <div className="flex-1 bg-slate-100 dark:bg-slate-900" />
        <div className="w-80 border-l border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-950" />
      </div>
    </div>
  );
}
