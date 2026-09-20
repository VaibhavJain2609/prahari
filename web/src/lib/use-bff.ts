"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";

// Generic GET hook for BFF reads — the small repeated shape behind
// SummaryStrip, the cameras table, the district filter list and the
// drawer's health-history slot. `path` is relative to /api/bff/ (e.g.
// "cameras/summary"); pass null to hold the fetch (e.g. a drawer that is
// closed). `params` become the query string; `purposeCode` rides as
// X-Purpose-Code for audited endpoints.
//
// Returns { data, error, loading, revalidate }: `revalidate` bumps a tick
// that refetches, so a PATCH in the drawer can refresh its own read without
// prop-drilling a refresh callback through the page.

export type BFFResult<T> = {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  revalidate: () => void;
};

export function useBFF<T>(
  path: string | null,
  opts: {
    params?: Record<string, string | number | undefined>;
    purposeCode?: string | null;
    refreshMs?: number;
  } = {},
): BFFResult<T> {
  const { params, purposeCode, refreshMs } = opts;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  // Params are a fresh object literal every render; key the effect on the
  // serialized form so {district: "x"} doesn't refetch in a loop.
  const query = new URLSearchParams(
    Object.entries(params ?? {})
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => [k, String(v)]),
  ).toString();

  const revalidate = useCallback(() => setTick((t) => t + 1), []);

  // Latest-fetch-wins plus AbortController: a stale response arriving after
  // a filter change must not overwrite the newer one.
  const seq = useRef(0);

  useEffect(() => {
    if (path == null) return;
    const controller = new AbortController();
    const mySeq = ++seq.current;

    async function load() {
      setLoading(true);
      try {
        const result = await api.get<T>(`${path}${query ? `?${query}` : ""}`, {
          purposeCode: purposeCode ?? undefined,
          signal: controller.signal,
        });
        if (mySeq !== seq.current) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (mySeq !== seq.current || controller.signal.aborted) return;
        setData(null);
        setError(err instanceof ApiError ? err : new ApiError(0, "request failed"));
      } finally {
        if (mySeq === seq.current) setLoading(false);
      }
    }

    load();
    const interval = refreshMs ? setInterval(load, refreshMs) : undefined;
    return () => {
      controller.abort();
      if (interval) clearInterval(interval);
    };
  }, [path, query, purposeCode, refreshMs, tick]);

  return { data, error, loading, revalidate };
}
