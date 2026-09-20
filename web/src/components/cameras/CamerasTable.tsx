"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, Camera, Lifecycle } from "@/lib/api";
import { useBFF } from "@/lib/use-bff";
import { timeAgo } from "@/lib/format";
import { HealthBadge } from "@/components/ui/Badge";

const LIMIT = 500;

export type CameraFilters = {
  district: string;
  department: string;
  state: string;
  lifecycle: string; // "active" | "absent" | "decommissioned" | "all"
  search: string;
  offset: number;
};

// The registry's estate list, filterable. Filters live in the URL (the
// parent mirrors them) so a filtered view is a shareable link and survives
// a reload.
export default function CamerasTable({
  filters,
  onFilters,
  onSelect,
  reloadTick = 0,
}: {
  filters: CameraFilters;
  onFilters: (next: Partial<CameraFilters>) => void;
  onSelect: (cameraId: string) => void;
  reloadTick?: number;
}) {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [searchDraft, setSearchDraft] = useState(filters.search);
  const seq = useRef(0);

  const { data: districts } = useBFF<{ district: string | null }[]>("gaps/districts");

  const { district, department, state, lifecycle, search, offset } = filters;

  // Debounce the free-text search into the URL — a param write per
  // keystroke is a fetch per keystroke.
  useEffect(() => {
    if (searchDraft === search) return;
    const t = setTimeout(() => onFilters({ search: searchDraft, offset: 0 }), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  useEffect(() => {
    const controller = new AbortController();
    const mySeq = ++seq.current;
    async function load() {
      setLoading(true);
      const base = {
        district: district || undefined,
        department: department || undefined,
        state: state || undefined,
        search: search || undefined,
        limit: LIMIT,
        offset,
      };
      try {
        let rows: Camera[];
        if (lifecycle === "all") {
          // The registry's `lifecycle` filter is single-valued and defaults
          // to active — there is no "every lifecycle" query. "All" here is
          // therefore three parallel scoped fetches merged client-side,
          // deduped on id in case a lifecycle transition lands mid-request.
          const per = await Promise.all(
            (["active", "absent", "decommissioned"] as Lifecycle[]).map((l) =>
              api.listCameras({ ...base, lifecycle: l }, { signal: controller.signal }),
            ),
          );
          const seen = new Map<string, Camera>();
          for (const row of per.flat()) if (!seen.has(row.id)) seen.set(row.id, row);
          rows = [...seen.values()];
        } else {
          rows = await api.listCameras(
            { ...base, lifecycle: lifecycle as Lifecycle },
            { signal: controller.signal },
          );
        }
        if (mySeq !== seq.current) return;
        setCameras(rows);
        setError(null);
      } catch (err) {
        if (mySeq !== seq.current || controller.signal.aborted) return;
        setCameras(null);
        setError(err instanceof ApiError ? err.message : "list failed");
      } finally {
        if (mySeq === seq.current) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [district, department, state, lifecycle, search, offset, reloadTick]);

  const input =
    "rounded border border-slate-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:focus:ring-slate-500";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 px-4 py-2">
        <select
          value={district}
          onChange={(e) => onFilters({ district: e.target.value, offset: 0 })}
          aria-label="Filter by district"
          className={input}
        >
          <option value="">all districts</option>
          {(districts ?? [])
            .map((d) => d.district)
            .filter((d): d is string => d != null)
            .map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
        </select>
        <input
          value={department}
          onChange={(e) => onFilters({ department: e.target.value, offset: 0 })}
          placeholder="department"
          aria-label="Filter by department"
          className={input}
        />
        <select
          value={state}
          onChange={(e) => onFilters({ state: e.target.value, offset: 0 })}
          aria-label="Filter by health state"
          className={input}
        >
          <option value="">any health</option>
          <option value="healthy">healthy</option>
          <option value="degraded">degraded</option>
          <option value="unreachable">unreachable</option>
          <option value="tampered">tampered</option>
          <option value="unknown">unknown</option>
        </select>
        <select
          value={lifecycle}
          onChange={(e) => onFilters({ lifecycle: e.target.value, offset: 0 })}
          aria-label="Filter by lifecycle"
          className={input}
        >
          <option value="active">active</option>
          <option value="absent">absent</option>
          <option value="decommissioned">decommissioned</option>
          <option value="all">all</option>
        </select>
        <input
          value={searchDraft}
          onChange={(e) => setSearchDraft(e.target.value)}
          placeholder="search id / site"
          aria-label="Search cameras"
          className={`${input} min-w-40 flex-1`}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">
        {error ? (
          <p className="text-xs text-red-600 dark:text-red-400">{error}</p>
        ) : (
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-white text-[11px] uppercase tracking-wide text-slate-400 dark:bg-slate-950">
              <tr>
                <th className="px-2 py-1 font-medium">External ID</th>
                <th className="px-2 py-1 font-medium">Site</th>
                <th className="px-2 py-1 font-medium">District</th>
                <th className="px-2 py-1 font-medium">Department</th>
                <th className="px-2 py-1 font-medium">Type</th>
                <th className="px-2 py-1 font-medium">Health</th>
                <th className="px-2 py-1 font-medium">Lifecycle</th>
                <th className="px-2 py-1 font-medium">Last frame</th>
              </tr>
            </thead>
            <tbody>
              {(cameras ?? []).map((c) => (
                <tr
                  key={c.id}
                  onClick={() => onSelect(c.id)}
                  className="cursor-pointer border-t border-slate-100 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-900"
                >
                  <td className="px-2 py-1 font-mono text-[11px] text-slate-700 dark:text-slate-300">
                    {c.external_id}
                  </td>
                  <td className="px-2 py-1 text-slate-800 dark:text-slate-200">
                    {c.site_name ?? "—"}
                  </td>
                  <td className="px-2 py-1 text-slate-600 dark:text-slate-400">
                    {c.district ?? "—"}
                  </td>
                  <td className="px-2 py-1 text-slate-600 dark:text-slate-400">
                    {c.department ?? "—"}
                  </td>
                  <td className="px-2 py-1 text-slate-600 dark:text-slate-400">{c.camera_type}</td>
                  <td className="px-2 py-1">
                    <HealthBadge state={c.health?.state ?? "unknown"} />
                  </td>
                  <td className="px-2 py-1 text-slate-600 dark:text-slate-400">{c.lifecycle}</td>
                  <td className="px-2 py-1 text-slate-500 dark:text-slate-400">
                    {c.health?.last_frame_at ? timeAgo(c.health.last_frame_at) : "never"}
                  </td>
                </tr>
              ))}
              {cameras && cameras.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-2 py-6 text-center text-slate-400">
                    No cameras match these filters.
                  </td>
                </tr>
              )}
              {!cameras && loading && (
                <tr>
                  <td colSpan={8} className="px-2 py-6 text-center text-slate-400">
                    <span className="animate-pulse">loading…</span>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-slate-200 px-4 py-2 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
        <span>
          {cameras ? `${offset + 1}–${offset + cameras.length}` : "…"}
          {loading && " · refreshing"}
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => onFilters({ offset: Math.max(0, offset - LIMIT) })}
            disabled={offset === 0}
            className="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700"
          >
            ‹ prev
          </button>
          <button
            onClick={() => onFilters({ offset: offset + LIMIT })}
            // A full page is the only "more exists" signal the API gives.
            disabled={!cameras || cameras.length < LIMIT}
            className="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700"
          >
            next ›
          </button>
        </div>
      </div>
    </div>
  );
}
