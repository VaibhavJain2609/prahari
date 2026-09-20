"use client";

import { createContext, useContext } from "react";
import { StreamState, useAlertStream, useSSEStatus } from "@/lib/alerts";

// One EventSource per console, mounted once in the (console) layout. The
// underlying stream is already a module-level singleton in lib/alerts.ts —
// this context is how the header's status dot and the alerts rail observe
// the *same* connection without each reaching for the module separately,
// and it gives tests a seam to substitute a fake stream state.
const AlertStreamContext = createContext<{ state: StreamState }>({ state: "connecting" });

export default function AlertStreamProvider({ children }: { children: React.ReactNode }) {
  // Subscribing here is what opens the singleton EventSource — the provider
  // mounts exactly once per console load, so the upstream subscription
  // count stays at one no matter how many panels listen.
  const state = useSSEStatus();
  return (
    <AlertStreamContext.Provider value={{ state }}>{children}</AlertStreamContext.Provider>
  );
}

// Connection state for the header dot ("live" / "reconnecting" / …).
export function useAlertStreamState(): StreamState {
  return useContext(AlertStreamContext).state;
}

// Event delivery for the rail — same shared stream, one subscription per
// mounted listener, zero extra EventSources.
export { useAlertStream };
