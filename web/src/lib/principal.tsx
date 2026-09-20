"use client";

import { createContext, useContext } from "react";
import { Org, Principal } from "@/lib/api";

// Who is signed in and which org they belong to, fetched once by the
// console layout. Every panel reads this instead of re-calling auth/me —
// one fetch per console load, and one place that decides what "unreachable"
// means.
export type PrincipalState = {
  principal: Principal;
  org: Org | null;
  orgs: Org[];
};

const PrincipalContext = createContext<PrincipalState | null>(null);

export const PrincipalProvider = PrincipalContext.Provider;

// Non-nullable: the console layout only renders children once a principal
// has loaded, so a panel calling this outside the layout (or before load)
// is a bug, and it should fail loudly rather than render half-signed-in.
export function usePrincipal(): PrincipalState {
  const ctx = useContext(PrincipalContext);
  if (!ctx) throw new Error("usePrincipal used outside the console layout");
  return ctx;
}
