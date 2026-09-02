// Mirrors services/registry/src/prahari_registry/models.py::HealthState.
// Values are the lowercase wire strings the geojson endpoint emits.
export type HealthState = "unknown" | "healthy" | "degraded" | "unreachable" | "tampered";

export const HEALTH_COLORS: Record<HealthState, string> = {
  healthy: "#22c55e",
  degraded: "#eab308",
  unreachable: "#94a3b8",
  tampered: "#ef4444",
  unknown: "#64748b",
};

export const HEALTH_LABELS: Record<HealthState, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  unreachable: "Unreachable",
  tampered: "Tampered",
  unknown: "Unknown",
};

export function healthColor(state: string): string {
  return HEALTH_COLORS[state as HealthState] ?? HEALTH_COLORS.unknown;
}
