import { useState } from "react";
import { listInvitations, createInvitation, revokeInvitation } from "../api.js";
import { PageState } from "../components/PageState.jsx";
import { usePageData } from "../hooks/usePageData.js";

function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

export function AdminInvitations() {
  const { data, loading, forbidden, error, retry } = usePageData(listInvitations);
  const invitations = data || [];
  const [email, setEmail] = useState("");
  const [ttlDays, setTtlDays] = useState(7);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleInvite(e) {
    e.preventDefault();
    const addr = email.trim().toLowerCase();
    if (!addr || busy) return;
    setBusy(true);
    setFormError("");
    try {
      await createInvitation(addr, ttlDays);
      setEmail("");
      retry();
    } catch (err) {
      setFormError(err.message || "Failed to create invitation.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(token, email) {
    if (!window.confirm(`Revoke the invitation for ${email}?`)) return;
    try {
      await revokeInvitation(token);
      retry();
    } catch (err) {
      setFormError(err.message || "Failed to revoke invitation.");
    }
  }

  return (
    <div className="admin-invitations">
      <h2>Invitations</h2>
      <p className="hint">
        Invite an email address so it can sign in via OAuth for the first time. Each invitation is
        single-use and expires after the configured number of days.
      </p>

      <form className="invite-form" onSubmit={handleInvite}>
        <input
          type="email"
          value={email}
          onChange={(e) => {
            setEmail(e.target.value);
            setFormError("");
          }}
          placeholder="user@example.com"
          required
        />
        <label>
          Expires in
          <input
            type="number"
            min={1}
            max={365}
            value={ttlDays}
            onChange={(e) => setTtlDays(Number(e.target.value))}
            style={{ width: "var(--space-8)", marginLeft: "var(--space-2)" }}
          />
          days
        </label>
        <button className="tap-target" disabled={busy || !email.trim()}>
          {busy ? "Sending…" : "Invite"}
        </button>
        {formError && <span className="gate-error">{formError}</span>}
      </form>

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        <div className="table-scroll">
          <table className="perf-jobs-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Invited by</th>
                <th>Expires</th>
                <th>Consumed</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {invitations.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ textAlign: "center", color: "var(--dim)" }}>
                    No invitations yet.
                  </td>
                </tr>
              )}
              {invitations.map((inv) => (
                <tr key={inv.id} className={inv.consumed_at ? "consumed" : ""}>
                  <td>{inv.email}</td>
                  <td>{inv.invited_by || "—"}</td>
                  <td>{fmtDate(inv.expires_at)}</td>
                  <td>{inv.consumed_at ? fmtDate(inv.consumed_at) : "Pending"}</td>
                  <td>
                    {!inv.consumed_at && (
                      <button
                        className="btn-ghost-danger tap-target"
                        onClick={() => handleRevoke(inv.token, inv.email)}
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </PageState>
    </div>
  );
}
