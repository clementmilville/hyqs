import { useEffect, useState } from "react";
import { verifyToken, setToken } from "../api.js";

export function OAuthGate({ onSave }) {
  const [providers, setProviders] = useState({ google: false, apple: false });
  const [breakGlassOpen, setBreakGlassOpen] = useState(false);
  const [token, setTokenInput] = useState("");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/auth/providers")
      .then((r) => r.json())
      .then((data) => setProviders(data))
      .catch(() => {});
  }, []);

  async function handleBreakGlass(e) {
    e?.preventDefault();
    const t = token.trim();
    if (!t || checking) return;
    setChecking(true);
    setError("");
    if (await verifyToken(t)) {
      setToken(t);
      onSave();
    } else {
      setError("Invalid token — the server rejected it.");
      setChecking(false);
    }
  }

  const hasProviders = providers.google || providers.apple;

  return (
    <div className="gate">
      <h1>Hyqs</h1>
      <p className="gate-subtitle">Your autonomous dev pipeline</p>
      {hasProviders && (
        <div className="oauth-buttons">
          {providers.google && (
            <a className="oauth-btn oauth-btn-google" href="/api/auth/oauth/google/redirect">
              Sign in with Google
            </a>
          )}
          {providers.apple && (
            <a className="oauth-btn oauth-btn-apple" href="/api/auth/oauth/apple/redirect">
              Sign in with Apple
            </a>
          )}
        </div>
      )}
      <div className="gate-footer">
        <button
          className="break-glass-link"
          type="button"
          onClick={() => setBreakGlassOpen((o) => !o)}
        >
          Admin access
        </button>
        {breakGlassOpen && (
          <form className="break-glass-form" onSubmit={handleBreakGlass}>
            <input
              type="password"
              value={token}
              onChange={(e) => {
                setTokenInput(e.target.value);
                setError("");
              }}
              placeholder="HYQS_WEB_TOKEN"
              autoFocus
            />
            <button disabled={checking || !token.trim()}>
              {checking ? "Checking…" : "Connect"}
            </button>
            {error && <p className="gate-error">{error}</p>}
          </form>
        )}
      </div>
    </div>
  );
}
