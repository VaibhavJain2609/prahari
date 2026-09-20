"use client";

import { useState } from "react";
import { api, ApiError, Org, OrgKind } from "@/lib/api";
import { usePrincipal } from "@/lib/principal";
import Panel from "@/components/ui/Panel";

// Org tree + sub-org creation (was AdminPanel's CreateOrg). The tree is
// derived from the flat org list by parent_id, ordered by path — the ltree
// path is the truth about nesting, parent_id just makes the grouping cheap.
export default function OrgSection({ orgs }: { orgs: Org[] }) {
  const { principal, org } = usePrincipal();
  return (
    <Panel title="Organization" caption={org ? `subtree of ${org.name}` : undefined}>
      <OrgTree orgs={orgs} rootId={principal.org_id} />
      <CreateOrg parentId={principal.org_id} />
    </Panel>
  );
}

// Render the caller's own org at depth 0 and descendants indented by path
// depth — the flat list already comes scoped to the caller's subtree.
function OrgTree({ orgs, rootId }: { orgs: Org[]; rootId: string }) {
  if (orgs.length === 0) {
    return <p className="mb-3 text-xs text-slate-400">No orgs in scope.</p>;
  }
  const root = orgs.find((o) => o.id === rootId);
  const baseDepth = (root?.path ?? "").split(".").filter(Boolean).length;
  const childrenOf = new Map<string | null, Org[]>();
  for (const o of orgs) {
    const list = childrenOf.get(o.parent_id) ?? [];
    list.push(o);
    childrenOf.set(o.parent_id, list);
  }
  const ordered: { org: Org; depth: number }[] = [];
  const walk = (o: Org) => {
    const depth = Math.max(0, o.path.split(".").filter(Boolean).length - baseDepth);
    ordered.push({ org: o, depth });
    for (const child of childrenOf.get(o.id) ?? []) walk(child);
  };
  if (root) walk(root);
  // Orphans in the scoped list (e.g. the root itself wasn't in scope of the
  // query) still render, flat, rather than disappearing.
  const rendered = new Set(ordered.map((e) => e.org.id));
  for (const o of orgs) {
    if (!rendered.has(o.id)) ordered.push({ org: o, depth: 0 });
  }

  return (
    <ul className="mb-3 max-h-48 space-y-0.5 overflow-y-auto text-xs">
      {ordered.map(({ org: o, depth }) => (
        <li key={o.id} style={{ paddingLeft: `${depth * 12}px` }} className="text-slate-600 dark:text-slate-300">
          <span className="font-medium text-slate-800 dark:text-slate-100">{o.name}</span>{" "}
          <span className="text-slate-400">
            {o.kind} · <span className="font-mono text-[10px]">{o.path}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function CreateOrg({ parentId }: { parentId: string }) {
  const [label, setLabel] = useState("");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<OrgKind>("local_body");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!label.trim() || !name.trim()) return;
    setBusy(true);
    setMessage(null);
    try {
      await api.createOrg({ parent_id: parentId, label: label.trim(), kind, name: name.trim() });
      setMessage({ ok: true, text: `${name} created.` });
      setLabel("");
      setName("");
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : "create failed" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-2 border-t border-slate-100 pt-2 dark:border-slate-800">
      <p className="text-xs font-medium text-slate-700 dark:text-slate-300">New sub-org</p>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="display name"
        aria-label="Display name"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <input
        value={label}
        onChange={(e) => setLabel(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
        placeholder="path label (e.g. zone_4)"
        aria-label="Path label"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      />
      <select
        value={kind}
        onChange={(e) => setKind(e.target.value as OrgKind)}
        aria-label="Org kind"
        className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500"
      >
        <option value="organization">organization</option>
        <option value="local_body">local body</option>
      </select>
      <button
        type="submit"
        disabled={busy || !label.trim() || !name.trim()}
        className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:bg-slate-100 dark:text-slate-900"
      >
        {busy ? "Creating…" : "Create org"}
      </button>
      {message && (
        <p className={`text-xs ${message.ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}
