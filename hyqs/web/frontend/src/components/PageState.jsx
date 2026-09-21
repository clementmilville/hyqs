import { Spinner } from "./Spinner.jsx";

// Shared three-state page wrapper (CONVENTIONS.md §9.4 / usePageData.js):
// forbidden -> access-denied message, error -> message + Retry button,
// loading -> Spinner. When none apply, renders `children`.
//
// Props:
//   forbidden: boolean — the caller lacks access to this data
//   error:     boolean — the fetch/stream failed for a non-forbidden reason
//   loading:   boolean — no data yet, still in flight
//   retry:     () => void — called when the user clicks Retry (required if `error`)
//   children:  ReactNode — rendered once forbidden/error/loading are all false
export function PageState({ forbidden, error, loading, retry, children }) {
  if (forbidden) {
    return <p className="hint">You don&apos;t have access to this data.</p>;
  }
  if (error) {
    return (
      <div className="fleet-error">
        <p className="hint">Couldn&apos;t connect to the server.</p>
        <button className="tap-target" onClick={retry}>
          Retry
        </button>
      </div>
    );
  }
  if (loading) {
    return <Spinner />;
  }
  return children;
}
