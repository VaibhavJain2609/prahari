"use client";

import { createContext, useContext, useEffect, useState } from "react";

// The operator's declared reason for audited access — a purpose category
// plus a case reference (FIR/eGujCop number, incident id, …). Every audited
// call sends it verbatim as X-Purpose-Code, and the BFF refuses the call
// without one, so the console asks once per session and reuses it.
//
// sessionStorage, deliberately: a purpose survives a reload mid-session but
// not a new tab or a browser restart — a stale "court-order:FIR-9" left
// lying around in localStorage would keep getting stamped onto audits long
// after the operator stopped thinking about that case.

export type PurposeAction = "plate-trace" | "investigation" | "court-order" | "other";

export type Purpose = {
  action: PurposeAction;
  ref: string;
};

export const PURPOSE_ACTIONS: { value: PurposeAction; label: string }[] = [
  { value: "plate-trace", label: "Plate trace" },
  { value: "investigation", label: "Investigation" },
  { value: "court-order", label: "Court order" },
  { value: "other", label: "Other" },
];

const STORAGE_KEY = "prahari-purpose";

const PurposeContext = createContext<{
  purpose: Purpose | null;
  setPurpose: (p: Purpose) => void;
  clearPurpose: () => void;
}>({
  purpose: null,
  setPurpose: () => {},
  clearPurpose: () => {},
});

// "<action>:<case ref>" — the wire format the audit log stores. A blank ref
// degrades to the bare action code (same convention as api.ts::purpose).
export function composePurpose(purpose: Purpose | null): string | null {
  if (!purpose) return null;
  const ref = purpose.ref.trim();
  return ref ? `${purpose.action}:${ref}` : purpose.action;
}

function readStored(): Purpose | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Purpose;
    if (!PURPOSE_ACTIONS.some((a) => a.value === parsed.action)) return null;
    return { action: parsed.action, ref: String(parsed.ref ?? "") };
  } catch {
    return null;
  }
}

export function PurposeProvider({ children }: { children: React.ReactNode }) {
  // Lazy-init from sessionStorage; the provider only ever mounts inside the
  // client-side console layout, so there is no SSR divergence to guard.
  const [purpose, setPurposeState] = useState<Purpose | null>(readStored);

  useEffect(() => {
    if (purpose) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(purpose));
    else window.sessionStorage.removeItem(STORAGE_KEY);
  }, [purpose]);

  return (
    <PurposeContext.Provider
      value={{
        purpose,
        setPurpose: setPurposeState,
        clearPurpose: () => setPurposeState(null),
      }}
    >
      {children}
    </PurposeContext.Provider>
  );
}

export function usePurpose() {
  return useContext(PurposeContext);
}

// Inline prompt used wherever an audited call is attempted without a
// purpose set — the camera drawer, and the trace dock when driven by a
// `?trace=` deep link. Compact by design: it sits inside panels, not modals.
export function PurposePrompt({ onSet }: { onSet?: (p: Purpose) => void }) {
  const { setPurpose } = usePurpose();
  const [action, setAction] = useState<PurposeAction>("investigation");
  const [ref, setRef] = useState("");

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const p = { action, ref: ref.trim() };
    setPurpose(p);
    onSet?.(p);
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-1.5 rounded border border-amber-300 bg-amber-50 p-2 dark:border-amber-800 dark:bg-amber-950/40"
    >
      <p className="text-[11px] font-medium text-amber-900 dark:text-amber-200">
        Purpose required — this access is audited.
      </p>
      <select
        value={action}
        onChange={(e) => setAction(e.target.value as PurposeAction)}
        aria-label="Purpose"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        {PURPOSE_ACTIONS.map((a) => (
          <option key={a.value} value={a.value}>
            {a.label}
          </option>
        ))}
      </select>
      <div className="flex gap-1.5">
        <input
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          placeholder="case reference (FIR / incident id)"
          aria-label="Case reference"
          className="min-w-0 flex-1 rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
        />
        <button
          type="submit"
          className="rounded bg-slate-900 px-2 py-1 text-xs font-medium text-white focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
        >
          Set
        </button>
      </div>
    </form>
  );
}
