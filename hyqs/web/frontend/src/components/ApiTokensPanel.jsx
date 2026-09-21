import { useEffect, useState } from "react";
import { GatedAction } from "../context.js";
import { listApiTokens, createApiToken, revokeApiToken } from "../api.js";

const TOKEN_ROLES = ["viewer", "automation_client", "contributor", "project_admin"];

export function ApiTokensPanel({ projectId, setNote, onChanged }) {
  const [tokens, setTokens] = useState([]);
  const [name, setName] = useState("");
  const [role, setRole] = useState("viewer");
  const [revealed, setRevealed] = useState(null);
  const [copied, setCopied] = useState(false);

  const reload = () =>
    listApiTokens(projectId)
      .then(setTokens)
      .catch((err) => setNote("⚠️ " + err.message));

  useEffect(() => {
    reload();
  }, [projectId]);

  const create = async (e) => {
    e.preventDefault();
    if (!name) return;
    try {
      const token = await createApiToken(projectId, name, role);
      setRevealed(token);
      setCopied(false);
      setName("");
      setRole("viewer");
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const revoke = async (tokenId) => {
    if (!window.confirm("Revoke this API token? Any integration using it will stop working.")) {
      return;
    }
    try {
      await revokeApiToken(projectId, tokenId);
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const copySecret = async () => {
    try {
      await navigator.clipboard.writeText(revealed.token);
      setCopied(true);
    } catch {
      setNote("⚠️ Couldn't copy to clipboard");
    }
  };

  const activeTokens = tokens.filter((t) => !t.revoked_at);

  return (
    <section className="tokens-panel">
      <h3>API Tokens</h3>
      {revealed && (
        <div className="token-reveal">
          <p className="hint">Copy this secret now — it will not be shown again.</p>
          <div className="token-reveal-row">
            <code className="token-reveal-secret">{revealed.token}</code>
            <button className="btn-secondary" onClick={copySecret}>
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <button className="link" onClick={() => setRevealed(null)}>
            Dismiss
          </button>
        </div>
      )}
      {activeTokens.length === 0 ? (
        <p className="hint">No API tokens yet.</p>
      ) : (
        <table className="members-table">
          <tbody>
            {activeTokens.map((t) => (
              <tr key={t.id}>
                <td>{t.name}</td>
                <td>
                  <span className="badge">{t.role}</span>
                </td>
                <td>•••• {t.last4}</td>
                <td>{t.created_at ? t.created_at.slice(0, 10) : ""}</td>
                <td>{t.last_used_at ? t.last_used_at.slice(0, 10) : "never used"}</td>
                <td>
                  <GatedAction require="manage_api_tokens">
                    <button className="link del" onClick={() => revoke(t.id)}>
                      Revoke
                    </button>
                  </GatedAction>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <GatedAction require="manage_api_tokens">
        <form className="member-add" onSubmit={create}>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Token name"
            required
          />
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            {TOKEN_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button>Create token</button>
        </form>
      </GatedAction>
    </section>
  );
}
