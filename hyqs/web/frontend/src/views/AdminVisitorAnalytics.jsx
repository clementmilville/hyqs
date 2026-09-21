import { useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { Eye } from "lucide-react";
import { getPageViewsSummary } from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";
import { EmptyState } from "../components/EmptyState.jsx";

const PAGE_VIEW_PERIOD_OPTIONS = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
];

// recharts renders Tooltip's contentStyle and axis `tick` props as SVG/attribute
// values that can't resolve CSS custom properties, so this literal must be kept
// in sync with tokens.css's --text-xs (11px).
const CHART_TICK_FONT_SIZE = 11;

const CHART_STYLE = {
  contentStyle: {
    background: "var(--panel)",
    border: "1px solid var(--line)",
    color: "var(--txt)",
    fontSize: CHART_TICK_FONT_SIZE,
  },
};

function localDateTime(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

// Rows recorded before the richer traits existed carry an empty string for
// viewport, colour scheme and OS version. The backend normalizes blanks to
// "unknown" for colour scheme and OS version but not for viewport, so this
// view normalizes any blank dimension label itself.
function blankToUnknown(value) {
  return value === "" || value == null ? "unknown" : value;
}

function DimensionTable({ title, columns, rows, rowKey }) {
  return (
    <div className="perf-chart-panel">
      <h4 className="perf-chart-title">{title}</h4>
      <div className="table-scroll">
        <table className="perf-jobs-table">
          <thead>
            <tr>
              {columns.map(({ label }) => (
                <th key={label}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={rowKey(row, index)}>
                {columns.map(({ key, render }) => (
                  <td key={key}>{render ? render(row[key], row) : (row[key] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CountryRow({ country, cities }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="u-stage-item">
      <div className="u-stage-row clickable" onClick={() => setOpen((o) => !o)}>
        <span className="expand">{open ? "▾" : "▸"}</span>
        <span className="u-stage-source">{country.country}</span>
        <span className="u-stage-cost">{country.views} views</span>
      </div>
      {open && (
        <ul className="u-jobs">
          {cities.map((c, i) => (
            <li key={`${c.city}-${i}`} className="u-job">
              <span className="u-name">{c.city}</span>
              <span className="u-tok">{c.views}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function AdminVisitorAnalytics() {
  const [rangeDays, setRangeDays] = useState(30);
  const [path, setPath] = useState("");
  const [showOsVersions, setShowOsVersions] = useState(false);

  const {
    data: summary,
    loading,
    forbidden,
    error,
    retry,
  } = usePageData(() => {
    const until = new Date().toISOString();
    const since = new Date(Date.now() - rangeDays * 24 * 60 * 60 * 1000).toISOString();
    const request = { since, until };
    if (path.trim()) request.path = path.trim();
    return getPageViewsSummary(request);
  }, [rangeDays, path]);

  const totals = summary?.totals;
  const byPath = summary?.by_path ?? [];
  const byHour = summary?.by_hour ?? [];
  const pagesPerVisitor = summary?.pages_per_visitor ?? [];
  const returning = summary?.returning ?? { single_day: 0, multi_day: 0 };
  const byViewport = summary?.by_viewport ?? [];
  const byColorScheme = summary?.by_color_scheme ?? [];
  const byLang = summary?.by_lang ?? [];
  const byOsVersion = summary?.by_os_version ?? [];
  const byCountry = summary?.by_country ?? [];
  const byCity = summary?.by_city ?? [];
  const byReferrer = summary?.by_referrer ?? [];
  const byRef = summary?.by_ref ?? [];
  const byDevice = summary?.by_device ?? [];
  const recent = summary?.recent ?? [];
  const countColumns = [
    { key: "views", label: "Views" },
    { key: "unique_visitors", label: "Unique visitors" },
  ];

  return (
    <div>
      <h3 className="fleet-section-head">Visitor analytics</h3>

      <div className="perf-filter-bar">
        <div className="perf-filter-field">
          <span className="perf-filter-label">Range</span>
          <div className="u-period" aria-label="Page view range">
            {PAGE_VIEW_PERIOD_OPTIONS.map(({ days, label }) => (
              <button
                key={days}
                className={`tap-target${rangeDays === days ? " active" : ""}`}
                onClick={() => setRangeDays(days)}
                aria-pressed={rangeDays === days}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <label className="perf-filter-field">
          <span className="perf-filter-label">Path (optional)</span>
          <input
            type="text"
            value={path}
            placeholder="All paths"
            onChange={(event) => setPath(event.target.value)}
          />
        </label>
      </div>

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        {summary &&
          (totals.views === 0 ? (
            <EmptyState
              icon={Eye}
              title="No page views in this range."
              hint="Privacy note: no IP address is stored."
            />
          ) : (
            <>
              <div className="perf-stat-row">
                {[
                  ["Unique visitors", totals.unique_visitors],
                  ["Views", totals.views],
                  ["Avg pages per visitor", totals.avg_pages_per_visitor ?? 0],
                  ["Days with traffic", totals.days_with_traffic],
                  ["Last visit", localDateTime(totals.last_seen)],
                ].map(([label, value]) => (
                  <div className="perf-stat-card" key={label}>
                    <div className="perf-stat-label">{label}</div>
                    <div className="perf-stat-value">{value}</div>
                  </div>
                ))}
              </div>

              <div className="perf-chart-panel">
                <h4 className="perf-chart-title">Views by day</h4>
                <div className="table-scroll">
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart
                      data={summary.by_day}
                      margin={{ top: 4, right: 16, left: 0, bottom: 4 }}
                    >
                      <XAxis
                        dataKey="day"
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                      />
                      <YAxis
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                        width={40}
                        allowDecimals={false}
                      />
                      <Tooltip {...CHART_STYLE} />
                      <Bar dataKey="views" fill="var(--accent)" name="Views" />
                      <Bar dataKey="unique_visitors" fill="var(--run)" name="Unique visitors" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              <DimensionTable
                title="Pages"
                columns={[
                  { key: "path", label: "Path" },
                  { key: "views", label: "Views" },
                  { key: "unique_visitors", label: "Unique visitors" },
                  {
                    key: "share",
                    label: "Share",
                    render: (_value, row) => (
                      <progress
                        value={row.views}
                        max={totals.views}
                        aria-label={`${Math.round((row.views / totals.views) * 100)}% of views`}
                      />
                    ),
                  },
                ]}
                rows={byPath}
                rowKey={(row) => row.path}
              />

              <h4 className="perf-chart-title">Where</h4>
              <ul className="u-stage-list">
                {byCountry.map((row) => (
                  <CountryRow
                    key={row.country}
                    country={row}
                    cities={byCity.filter((c) => c.country === row.country)}
                  />
                ))}
              </ul>
              <DimensionTable
                title="Referrers"
                columns={[
                  { key: "referrer_host", label: "Referrer" },
                  { key: "views", label: "Views" },
                ]}
                rows={byReferrer}
                rowKey={(row, index) => `${row.referrer_host}-${index}`}
              />

              <h4 className="perf-chart-title">Who sent them</h4>
              <DimensionTable
                title="By ref"
                columns={[{ key: "ref", label: "Ref" }, ...countColumns]}
                rows={byRef}
                rowKey={(row, index) => `${row.ref}-${index}`}
              />

              <h4 className="perf-chart-title">Devices</h4>
              <div className="perf-trends-grid">
                <DimensionTable
                  title="Viewport"
                  columns={[
                    {
                      key: "viewport",
                      label: "Viewport",
                      render: (value) => blankToUnknown(value),
                    },
                    ...countColumns,
                  ]}
                  rows={byViewport}
                  rowKey={(row) => row.viewport}
                />
                <DimensionTable
                  title="Colour scheme"
                  columns={[
                    {
                      key: "color_scheme",
                      label: "Colour scheme",
                      render: (value) => blankToUnknown(value),
                    },
                    { key: "views", label: "Views" },
                  ]}
                  rows={byColorScheme}
                  rowKey={(row) => row.color_scheme}
                />
                <DimensionTable
                  title="Language"
                  columns={[{ key: "lang", label: "Language" }, ...countColumns]}
                  rows={byLang}
                  rowKey={(row) => row.lang}
                />
                <DimensionTable
                  title="By device"
                  columns={[
                    { key: "os_family", label: "OS family" },
                    { key: "ua_family", label: "Browser family" },
                    ...countColumns,
                  ]}
                  rows={byDevice}
                  rowKey={(row, index) => `${row.os_family}-${row.ua_family}-${index}`}
                />
              </div>
              <button
                className="btn-secondary"
                type="button"
                aria-expanded={showOsVersions}
                onClick={() => setShowOsVersions((visible) => !visible)}
              >
                {showOsVersions ? "Hide OS versions" : "Show OS versions"}
              </button>
              {showOsVersions && (
                <DimensionTable
                  title="OS versions"
                  columns={[
                    { key: "os_family", label: "OS family" },
                    {
                      key: "os_version",
                      label: "OS version",
                      render: (value) => blankToUnknown(value),
                    },
                    { key: "views", label: "Views" },
                  ]}
                  rows={byOsVersion}
                  rowKey={(row, index) => `${row.os_family}-${row.os_version}-${index}`}
                />
              )}

              <div className="perf-chart-panel">
                <h4 className="perf-chart-title">When</h4>
                <div className="table-scroll">
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={byHour} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                      <XAxis
                        dataKey="hour"
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                      />
                      <YAxis
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                        width={40}
                        allowDecimals={false}
                      />
                      <Tooltip {...CHART_STYLE} />
                      <Bar dataKey="views" fill="var(--accent)" name="Views" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>

              <div className="perf-chart-panel">
                <h4 className="perf-chart-title">Depth</h4>
                <DimensionTable
                  title="Pages per visitor"
                  columns={[
                    { key: "pages", label: "Pages" },
                    { key: "visitors", label: "Visitors" },
                  ]}
                  rows={pagesPerVisitor}
                  rowKey={(row) => row.pages}
                />
                <div className="perf-stat-row">
                  <div className="perf-stat-card">
                    <div className="perf-stat-label">Single-day</div>
                    <div className="perf-stat-value">{returning.single_day ?? 0}</div>
                  </div>
                  <div className="perf-stat-card">
                    <div className="perf-stat-label">Multi-day</div>
                    <div className="perf-stat-value">{returning.multi_day ?? 0}</div>
                  </div>
                </div>
                <p className="dim">
                  Returning visitors are a floor, not a count, because the salt rotates daily.
                </p>
              </div>

              <div className="perf-chart-panel">
                <h4 className="perf-chart-title">Recent visits</h4>
                <ul className="u-stage-list">
                  {recent.map((visit, index) => (
                    <li className="u-stage-item" key={`${visit.ts}-${visit.path}-${index}`}>
                      <div className="u-stage-row">
                        <span className="u-stage-source">{localDateTime(visit.ts)}</span>
                        <span className="u-stage-cost">{visit.path}</span>
                      </div>
                      <div className="u-job">
                        {[visit.country, visit.city].filter(Boolean).join(" / ") ||
                          "Unknown location"}
                        {" · "}ref {visit.ref || "direct"}
                        {" · "}
                        {visit.os_family || "Unknown OS"} / {visit.ua_family || "Unknown browser"}
                        {" · "}
                        {visit.lang || "Unknown language"}
                        {" · "}
                        {visit.screen || "Unknown screen"}
                      </div>
                    </li>
                  ))}
                </ul>
                <p className="dim">
                  Privacy: no IP address is stored; the visitor key is a salted hash that rotates
                  every day.
                </p>
              </div>
            </>
          ))}
      </PageState>
    </div>
  );
}
