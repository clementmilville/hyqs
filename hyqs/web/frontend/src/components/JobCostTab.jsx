import { getJobUsage } from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "./PageState.jsx";

function sumRows(rows) {
  return rows.reduce(
    (acc, r) => ({
      input_tokens: acc.input_tokens + (r.input_tokens || 0),
      output_tokens: acc.output_tokens + (r.output_tokens || 0),
      cost_usd: acc.cost_usd + (r.cost_usd || 0),
    }),
    { input_tokens: 0, output_tokens: 0, cost_usd: 0 }
  );
}

export function JobCostTab({ job }) {
  const usage = usePageData(() => getJobUsage(job.id), [job.id]);

  return (
    <div className="job-cost-tab">
      <PageState
        forbidden={usage.forbidden}
        error={usage.error}
        loading={usage.loading}
        retry={usage.retry}
      >
        <JobCostTable rows={usage.data ?? []} />
      </PageState>
    </div>
  );
}

function JobCostTable({ rows }) {
  if (rows.length === 0) {
    return <div className="tab-empty">No usage recorded yet.</div>;
  }

  const total = sumRows(rows);

  return (
    <div className="cost-table-wrap">
      <table className="cost-table">
        <thead>
          <tr>
            <th>Stage</th>
            <th>Model</th>
            <th>Provider</th>
            <th>Input tokens</th>
            <th>Output tokens</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.source}-${r.model}-${r.provider}-${i}`}>
              <td>{r.source}</td>
              <td>{r.model}</td>
              <td>{r.provider}</td>
              <td>{r.input_tokens}</td>
              <td>{r.output_tokens}</td>
              <td>${r.cost_usd.toFixed(4)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>Total</td>
            <td />
            <td />
            <td>{total.input_tokens}</td>
            <td>{total.output_tokens}</td>
            <td>${total.cost_usd.toFixed(4)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
