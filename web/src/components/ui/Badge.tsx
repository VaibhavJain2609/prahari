import { HEALTH_COLORS, HEALTH_LABELS, HealthState } from "@/lib/health";
import { enumLabel } from "@/lib/format";

// Small status chips, one per enum family the console renders. Each maps a
// wire value to a colour; unknown values fall back to slate rather than
// crashing the row they sit in.

const BASE = "inline-block rounded px-1 py-px text-[10px] font-semibold";

export function HealthBadge({ state }: { state: string }) {
  const known = state in HEALTH_LABELS;
  const color = HEALTH_COLORS[(state as HealthState)] ?? HEALTH_COLORS.unknown;
  return (
    <span
      className={BASE}
      style={{ backgroundColor: color, color: "#fff" }}
      title={state}
    >
      {known ? HEALTH_LABELS[state as HealthState] : enumLabel(state)}
    </span>
  );
}

const PRIORITY_STYLES: Record<string, string> = {
  ALERT_PRIORITY_CRITICAL: "bg-red-600 text-white",
  ALERT_PRIORITY_HIGH: "bg-orange-500 text-white",
  ALERT_PRIORITY_MEDIUM: "bg-amber-400 text-amber-950",
  ALERT_PRIORITY_LOW: "bg-slate-300 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
};

export function PriorityBadge({ priority }: { priority: string }) {
  return (
    <span
      className={`${BASE} ${
        PRIORITY_STYLES[priority] ??
        "bg-slate-300 text-slate-800 dark:bg-slate-700 dark:text-slate-100"
      }`}
    >
      {enumLabel(priority, "ALERT_PRIORITY_").toUpperCase()}
    </span>
  );
}

const LINK_KIND_STYLES: Record<string, string> = {
  plate: "bg-sky-100 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
  bridged: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
};

// Route-hop link provenance: "plate" = same plate seen at both ends,
// "bridged" = correlation stitched a gap on appearance — a weaker claim,
// and the badge is how the operator sees which is which.
export function LinkKindBadge({ kind }: { kind: string | null | undefined }) {
  if (!kind) return null;
  return (
    <span
      className={`${BASE} ${
        LINK_KIND_STYLES[kind] ??
        "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200"
      }`}
    >
      {enumLabel(kind)}
    </span>
  );
}
