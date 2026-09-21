import { FilesList } from "./FilesList.jsx";

export function EventDetail({ ev, jobId, onViewDiff }) {
  const d = ev.detail || {};
  if (ev.stage === "plan") {
    return d.stories?.length ? (
      <ol className="stories">
        {d.stories.map((s, i) => (
          <li key={i}>
            <b>{s.title || s.id}</b>
            {s.task ? ` — ${s.task}` : ""}
            {s.acceptance ? <span className="accept"> ✓ {s.acceptance}</span> : null}
          </li>
        ))}
      </ol>
    ) : null;
  }
  if (ev.stage === "build" || ev.stage === "fix") {
    return (
      <div>
        {ev.summary && <div className="evsummary">{ev.summary}</div>}
        {d.commit?.hash && (
          <div className="commit">
            ⎇ <code>{d.commit.hash}</code> {d.commit.subject}
          </div>
        )}
        {ev.stage === "fix" && d.trigger && (
          <details className="trigger">
            <summary>what triggered this fix</summary>
            <pre>{d.trigger}</pre>
          </details>
        )}
        <FilesList files={d.files} />
        {d.patch ? (
          <details>
            <summary>view diff</summary>
            <pre className="diff">
              {d.patch}
              {d.patch_truncated ? "\n… (truncated)" : ""}
            </pre>
          </details>
        ) : d.files?.length > 0 ? (
          <button className="link" onClick={() => onViewDiff(jobId)}>
            view full diff
          </button>
        ) : null}
        {d.pr_url && (
          <div>
            <a className="prlink" href={d.pr_url} target="_blank" rel="noreferrer">
              🔗 PR
            </a>
          </div>
        )}
      </div>
    );
  }
  if (ev.stage === "merge") {
    return (
      <div>
        {ev.summary && <div className="evsummary">{ev.summary}</div>}
        {d.pr_url && (
          <a className="prlink" href={d.pr_url} target="_blank" rel="noreferrer">
            🔗 view PR
          </a>
        )}
      </div>
    );
  }
  if (ev.stage === "test") {
    return (
      <div>
        {d.command && (
          <div className="cmd">
            <code>{d.command}</code>
          </div>
        )}
        {d.output && (
          <details open={ev.status === "failed"}>
            <summary>output</summary>
            <pre className="output">{d.output}</pre>
          </details>
        )}
      </div>
    );
  }
  if (ev.stage === "review") {
    return (
      <div>
        {ev.summary && <div className="evsummary">{ev.summary}</div>}
        {d.findings?.length > 0 && (
          <ul className="findings">
            {d.findings.map((f, i) => (
              <li key={i}>
                <span className={`sev ${f.severity}`}>{f.severity}</span> {f.note}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (ev.stage === "security") {
    return (
      <div>
        {ev.summary && <div className="evsummary">{ev.summary}</div>}
        {d.verdict && <div className={`security-verdict ${d.verdict}`}>Final verdict: {d.verdict}</div>}
        {d.findings?.length > 0 && (
          <ul className="findings">
            {d.findings.map((f, i) => (
              <li key={i}>
                <span className={`sev ${f.severity}`}>{f.severity}</span> {f.note}
                {f.file ? <span className="finding-file"> — {f.file}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  return ev.summary ? <div className="evsummary">{ev.summary}</div> : null;
}
