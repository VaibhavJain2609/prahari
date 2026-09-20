"use client";

import { useEffect, useSyncExternalStore } from "react";

// The BFF's SSE alert stream, shared. The header's status dot and the
// AlertPanel both need the same connection — two EventSources would be two
// upstream subscriptions for one console — so the source is a module-level
// singleton created on first subscription and kept for the life of the
// page. Same-origin through the proxy, so the session cookie attaches
// automatically; the BFF scopes every alert to the caller's org subtree
// before it reaches the wire.

export type StreamState = "connecting" | "open" | "unavailable" | "error";

let source: EventSource | null = null;
let state: StreamState = "connecting";
let alertSeq = 0;

const stateListeners = new Set<() => void>();
const alertListeners = new Set<(raw: Record<string, unknown>, seq: number) => void>();

function setState(next: StreamState) {
  if (state === next) return;
  state = next;
  stateListeners.forEach((listener) => listener());
}

function ensureSource() {
  if (source || typeof window === "undefined") return;
  const es = new EventSource("/api/bff/alerts/stream");
  source = es;

  es.onopen = () => setState("open");

  es.addEventListener("alert", (event) => {
    let raw: Record<string, unknown>;
    try {
      raw = JSON.parse((event as MessageEvent).data);
    } catch {
      return; // a malformed frame is dropped, not fatal to the stream
    }
    alertSeq += 1;
    alertListeners.forEach((listener) => listener(raw, alertSeq));
  });

  es.onerror = () => {
    // The BFF returns 503 (no redis_url configured) as a plain HTTP error
    // before any SSE framing starts, which EventSource surfaces here
    // indistinguishably from a dropped connection — both read as "not
    // currently available" from the console's point of view.
    setState(es.readyState === EventSource.CLOSED ? "unavailable" : "error");
  };
}

export function subscribeSSEStatus(listener: () => void): () => void {
  ensureSource();
  stateListeners.add(listener);
  return () => {
    stateListeners.delete(listener);
  };
}

export function getSSEStatus(): StreamState {
  return state;
}

// Latest alert received on the shared stream. `seq` rises monotonically so
// consumers can tell "no alert yet" from "same alert re-rendered".
export function subscribeAlerts(
  listener: (raw: Record<string, unknown>, seq: number) => void,
): () => void {
  ensureSource();
  alertListeners.add(listener);
  return () => {
    alertListeners.delete(listener);
  };
}

export function useSSEStatus(): StreamState {
  return useSyncExternalStore(subscribeSSEStatus, getSSEStatus, () => "connecting");
}

export function useAlertStream(
  onAlert: (raw: Record<string, unknown>) => void,
): void {
  useEffect(() => subscribeAlerts((raw) => onAlert(raw)), [onAlert]);
}
