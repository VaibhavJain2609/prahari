// Titled card wrapper shared by rail panels and admin sections (extracted
// from Sidebar). The panel owns the chrome — border, title, optional
// caption — so the content inside stays layout-agnostic.
export default function Panel({
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
