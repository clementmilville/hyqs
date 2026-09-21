// Generic day-grouped feed: buckets already-ordered `items` into calendar-day
// groups and renders one header per day, delegating row rendering to
// `renderRow`. Extracted from ChangelogTab.jsx's day-grouping so other
// screens (History/Audit/Remediation) can reuse the same day-header +
// row shape instead of re-deriving it.
//
// Props:
//   items: T[]                  — already ordered (typically newest-first)
//   getDate: (item: T) => string | Date | null | undefined
//   renderRow: (item: T, index: number) => ReactNode
//   renderCountChip?: (group: { day: string, items: T[] }) => ReactNode
//       — an optional coalesced-burst count chip rendered in the day header
//         (mirrors `.changelog-change-count`)
//   dayGroupClassName?: string  (default "feed-list-day-group")
//   dayHeaderClassName?: string (default "feed-list-day-header")
//   emptyHint?: string — rendered as a <p className="hint"> when `items` is empty

function dayKey(date) {
  if (!date) return "unknown";
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return "unknown";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()
  ).padStart(2, "0")}`;
}

export function formatDayHeader(day) {
  if (day === "unknown") return "Unknown date";
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

// Buckets `items` into { day, items } groups, preserving input order within
// and across groups, keyed by the calendar day of `getDate(item)`.
export function groupByDay(items, getDate) {
  const groups = [];
  const indexByDay = new Map();
  for (const item of items) {
    const day = dayKey(getDate(item));
    if (!indexByDay.has(day)) {
      indexByDay.set(day, groups.length);
      groups.push({ day, items: [] });
    }
    groups[indexByDay.get(day)].items.push(item);
  }
  return groups;
}

export function FeedList({
  items,
  getDate,
  renderRow,
  renderCountChip,
  dayGroupClassName = "feed-list-day-group",
  dayHeaderClassName = "feed-list-day-header",
  emptyHint,
}) {
  const groups = groupByDay(items, getDate);
  if (groups.length === 0) {
    return emptyHint ? <p className="hint">{emptyHint}</p> : null;
  }
  return (
    <>
      {groups.map((group) => (
        <div className={dayGroupClassName} key={group.day}>
          <div className={dayHeaderClassName}>
            {formatDayHeader(group.day)}
            {renderCountChip?.(group)}
          </div>
          {group.items.map((item, i) => renderRow(item, i))}
        </div>
      ))}
    </>
  );
}
