import { useEffect, useState } from "react";
import { Plug } from "lucide-react";
import { listProviders, clearProviderPause } from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";
import { EmptyState } from "../components/EmptyState.jsx";

export function AdminProviders() {
  const { data, loading, forbidden, error, retry } = usePageData(listProviders);
  const providers = data || [];
  const [clearing, setClearing] = useState(null);
  const [note, setNote] = useState("");

  useEffect(() => {
    const id = setInterval(retry, 5000);
    return () => clearInterval(id);
  }, [retry]);

  async function handleClear(provider) {
    if (clearing) return;
    setClearing(provider);
    setNote("");
    try {
      await clearProviderPause(provider);
      setNote(`Cleared pause for ${provider}.`);
      retry();
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setClearing(null);
    }
  }

  return (
    <div className="fleet">
      <h3 className="fleet-section-head">Providers</h3>
      {note && <p className="note">{note}</p>}
      <PageState
        forbidden={forbidden}
        error={error && data === null}
        loading={loading && data === null}
        retry={retry}
      >
        {providers.length === 0 ? (
          <EmptyState icon={Plug} title="No providers configured." />
        ) : (
          <div className="cards">
            {providers.map((p) => (
              <div key={p.provider} className="card" style={{ cursor: "default" }}>
                <div className="card-top">
                  <span className="card-name">{p.provider}</span>
                  <span className={`badge ${p.paused ? "bad" : "ok"}`}>
                    {p.paused ? "paused" : "active"}
                  </span>
                </div>
                <div className="card-meta">
                  {p.paused_until_iso
                    ? `Resumes at ${new Date(p.paused_until_iso).toLocaleString()}`
                    : "—"}
                </div>
                <button
                  disabled={!p.paused || clearing === p.provider}
                  onClick={() => handleClear(p.provider)}
                  className="cancel-btn tap-target"
                >
                  {clearing === p.provider ? "…" : "Clear"}
                </button>
              </div>
            ))}
          </div>
        )}
      </PageState>
    </div>
  );
}
