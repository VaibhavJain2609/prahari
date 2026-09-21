"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useBFF } from "@/lib/use-bff";
import Panel from "@/components/ui/Panel";

// The watchlist's operational view: how many entries the match engine is
// matching against right now, and the one control the console exposes —
// reload. Adding/removing plates stays a file operation on the mounted CSV
// (docs/DAY3-DESIGN.md §4): the console reloads, it does not edit — a
// write path here would need its own audit and liveness story, which is a
// different feature.
export default function WatchlistSection() {
  const { data: summary, error, loading, revalidate } = useBFF<{
    entries: number;
    skeleton_buckets: number;
    bloom_size_bits: number;
    bloom_hash_count: number;
    bloom_false_positive_rate: number;
  }>("watchlist/summary");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reloaded, setReloaded] = useState<number | null>(null);

  async function onReload() {
    setBusy(true);
    setActionError(null);
    try {
      const result = await api.reloadWatchlist();
      setReloaded(result.entries);
      revalidate();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "reload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Watchlist">
      {loading && <p className="text-xs text-slate-400">loading…</p>}
      {error && (
        <p className="text-xs text-red-600 dark:text-red-400">{error.message}</p>
      )}
      {summary && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-slate-500 dark:text-slate-400">entries</dt>
          <dd className="font-mono text-slate-800 dark:text-slate-200">{summary.entries}</dd>
          <dt className="text-slate-500 dark:text-slate-400">skeleton buckets</dt>
          <dd className="font-mono text-slate-800 dark:text-slate-200">
            {summary.skeleton_buckets}
          </dd>
          <dt className="text-slate-500 dark:text-slate-400">bloom filter</dt>
          <dd className="font-mono text-slate-800 dark:text-slate-200">
            {summary.bloom_size_bits} bits · {summary.bloom_hash_count} hashes · fp{" "}
            {(summary.bloom_false_positive_rate * 100).toFixed(2)}%
          </dd>
        </dl>
      )}
      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={onReload}
          disabled={busy}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-slate-400 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          {busy ? "Reloading…" : "Reload from file"}
        </button>
        {reloaded != null && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400">
            {reloaded} entries loaded
          </span>
        )}
      </div>
      {actionError && (
        <p className="mt-1 text-xs text-red-600 dark:text-red-400">{actionError}</p>
      )}
      <p className="mt-2 text-[10px] text-slate-400 dark:text-slate-500">
        Entries change via the mounted watchlist file; reload re-reads it.
        Every reload is an audited mutation.
      </p>
    </Panel>
  );
}
