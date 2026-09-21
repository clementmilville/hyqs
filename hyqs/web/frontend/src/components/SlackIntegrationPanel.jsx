import { useEffect, useState } from "react";
import { GatedAction } from "../context.js";
import {
  listProjectWebhooks,
  createProjectWebhook,
  setWebhookActive,
  deleteWebhook,
  getSlackCredentialStatus,
  setSlackCredential,
} from "../api.js";

const CHANNEL_ID_RE = /^[cg][a-z0-9]{6,}$/i;

export function SlackIntegrationPanel({ projectId, setNote, onChanged }) {
  const [status, setStatus] = useState(null);
  const [channels, setChannels] = useState([]);
  const [botToken, setBotToken] = useState("");
  const [channelId, setChannelId] = useState("");

  const reload = () => {
    getSlackCredentialStatus(projectId)
      .then(setStatus)
      .catch((err) => setNote("⚠️ " + err.message));
    listProjectWebhooks(projectId)
      .then((webhooks) => setChannels(webhooks.filter((w) => w.kind === "slack")))
      .catch((err) => setNote("⚠️ " + err.message));
  };

  useEffect(() => {
    reload();
  }, [projectId]);

  const saveToken = async (e) => {
    e.preventDefault();
    if (!botToken.trim()) return;
    try {
      await setSlackCredential(projectId, botToken.trim());
      setBotToken("");
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const addChannel = async (e) => {
    e.preventDefault();
    if (!CHANNEL_ID_RE.test(channelId.trim())) return;
    try {
      await createProjectWebhook(projectId, {
        url: channelId.trim(),
        event_type: "deploy",
        kind: "slack",
      });
      setChannelId("");
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const toggleChannel = async (webhook) => {
    try {
      await setWebhookActive(webhook.id, !webhook.active);
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const removeChannel = async (webhook) => {
    if (!window.confirm(`Remove Slack channel "${webhook.url}"?`)) return;
    try {
      await deleteWebhook(webhook.id);
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  return (
    <section className="tokens-panel">
      <h3>Slack Integration</h3>
      <div>
        <span className={`badge ${status?.configured ? "ok" : "bad"}`}>
          {status?.configured ? "Configured ✓" : "Not configured"}
        </span>
      </div>
      <GatedAction require="manage_webhooks">
        <form className="member-add" onSubmit={saveToken}>
          <input
            type="password"
            value={botToken}
            onChange={(e) => setBotToken(e.target.value)}
            placeholder="Slack bot token (xoxb-...)"
          />
          <button disabled={!botToken.trim()}>Save</button>
        </form>
      </GatedAction>

      {channels.length === 0 ? (
        <p className="hint">No Slack channels configured yet.</p>
      ) : (
        <table className="members-table">
          <tbody>
            {channels.map((w) => (
              <tr key={w.id}>
                <td>{w.url}</td>
                <td>
                  <span className={`badge ${w.active ? "active" : ""}`}>
                    {w.active ? "active" : "inactive"}
                  </span>
                </td>
                <td>
                  <GatedAction require="manage_webhooks">
                    <button className="link" onClick={() => toggleChannel(w)}>
                      {w.active ? "Disable" : "Enable"}
                    </button>
                  </GatedAction>
                </td>
                <td>
                  <GatedAction require="manage_webhooks">
                    <button className="link del" onClick={() => removeChannel(w)}>
                      Delete
                    </button>
                  </GatedAction>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <GatedAction require="manage_webhooks">
        <form className="member-add" onSubmit={addChannel}>
          <input
            value={channelId}
            onChange={(e) => setChannelId(e.target.value)}
            placeholder="C0123456"
          />
          <button disabled={!CHANNEL_ID_RE.test(channelId.trim())}>Add channel</button>
        </form>
      </GatedAction>
    </section>
  );
}
