"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { usePrincipal } from "@/lib/principal";
import { can } from "@/lib/rbac";
import { useAlertStreamState } from "@/components/AlertStreamProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";

// Sticky console chrome: wordmark + which board this is (from the signed-in
// org's kind, never a client-side guess), primary nav, stream health, who
// is signed in, theme, sign out. h-12 so the strip below gets the rest.
export default function ConsoleHeader() {
  const { principal, org } = usePrincipal();
  const pathname = usePathname();
  const router = useRouter();
  const stream = useAlertStreamState();

  async function onLogout() {
    await api.logout().catch(() => undefined);
    router.replace("/login");
  }

  const links: { href: string; label: string; badge?: string; hidden?: boolean }[] = [
    { href: "/", label: "Console" },
    { href: "/cameras", label: "Cameras" },
    // Live alerts are the rail on the console; /alerts is the persisted
    // history off the match engine's AlertStore.
    { href: "/alerts", label: "Alerts" },
    { href: "/admin", label: "Admin", hidden: !can(principal, "admin") },
  ];

  return (
    <header className="sticky top-0 z-20 flex h-12 items-center justify-between border-b border-slate-200 bg-white px-4 dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-center gap-6">
        <div className="flex items-baseline gap-3">
          <Link
            href="/"
            className="text-lg font-semibold text-slate-900 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:text-slate-50"
          >
            PRAHARI
          </Link>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {org ? `${boardLabel(org.kind)} — ${org.name}` : "Gujarat Sentinel"}
          </span>
        </div>
        <nav aria-label="Console" className="flex items-center gap-1 text-xs">
          {links
            .filter((l) => !l.hidden)
            .map((l) => {
              const active =
                l.href === "/" ? pathname === "/" : pathname.startsWith(l.href);
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  aria-current={active ? "page" : undefined}
                  className={`rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-slate-400 ${
                    active
                      ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
                  }`}
                >
                  {l.label}
                  {l.badge && (
                    <span className="ml-1 rounded bg-amber-100 px-1 text-[9px] font-semibold uppercase text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                      {l.badge}
                    </span>
                  )}
                </Link>
              );
            })}
        </nav>
      </div>
      <div className="flex items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
        <span
          role="status"
          aria-label={`alert stream ${sseLabel(stream)}`}
          title={`Alert stream: ${sseLabel(stream)}`}
          className={`inline-block h-2 w-2 rounded-full ${
            stream === "open"
              ? "bg-emerald-500"
              : stream === "connecting" || stream === "error"
                ? "bg-amber-500"
                : "bg-red-500"
          }`}
        />
        <ThemeToggle />
        <span>
          {principal.subject} · {principal.role}
        </span>
        <button
          onClick={onLogout}
          className="rounded border border-slate-300 px-2 py-1 text-slate-700 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}

function boardLabel(kind: string): string {
  if (kind === "state") return "Statewide";
  if (kind === "organization") return "Organization";
  return "Local body";
}

function sseLabel(state: string): string {
  if (state === "open") return "live";
  if (state === "connecting") return "connecting";
  if (state === "error") return "reconnecting";
  return "unavailable";
}
