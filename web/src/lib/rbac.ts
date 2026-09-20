import { Principal, Role } from "@/lib/api";

// Role gates for what the console *renders*. These are never the actual
// authorization decision — the BFF enforces every one of them server-side
// (PrincipalDep/OperatorDep/AdminDep) — they exist so a viewer isn't shown
// a form that can only fail, and an admin-only nav item doesn't tease a
// role that can't use it. Ordering: viewer < operator < admin.
const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

export function can(principal: Principal | null | undefined, required: Role): boolean {
  if (!principal) return false;
  return RANK[principal.role] >= RANK[required];
}
