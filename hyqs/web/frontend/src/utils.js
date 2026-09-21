function _toMs(s) {
  const hasZone = s.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(s);
  return new Date(hasZone ? s : s + "Z").getTime();
}

export function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - _toMs(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function dur(a, b) {
  if (!a || !b) return "";
  const s = Math.max(0, (_toMs(b) - _toMs(a)) / 1000);
  if (s < 1) return "<1s";
  if (s < 60) return `${Math.round(s)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function elapsed(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function periodToSince(period) {
  if (period === "week") {
    const d = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);
    return d.toISOString();
  }
  if (period === "month") {
    const d = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000);
    return d.toISOString();
  }
  return undefined;
}
