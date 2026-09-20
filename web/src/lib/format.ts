// Small display helpers shared across panels.

// Alerts are judged by when they were raised upstream, not when this tab
// happened to receive them — a reconnecting stream would otherwise redate
// every alert it replays. Unparseable input is shown as-is rather than
// collapsed to "NaNs ago".
export function timeAgo(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// Enum de-prefixing: proto enums arrive as screaming-snake wire strings
// ("ALERT_PRIORITY_CRITICAL", "WATCHLIST_REASON_MISSING_PERSON"). The prefix
// is passed explicitly rather than guessed — the meaningful tail can itself
// contain underscores (MISSING_PERSON), so "drop segments" rules mangle it.
export function dePrefix(value: string, prefix: string): string {
  return value.startsWith(prefix) ? value.slice(prefix.length) : value;
}

// "WATCHLIST_REASON_MISSING_PERSON" + "WATCHLIST_REASON_" → "missing person".
export function enumLabel(value: string, prefix = ""): string {
  return dePrefix(value, prefix).replaceAll("_", " ").toLowerCase();
}
