import { useEffect, useState } from "react";
import { GatedAction } from "../context.js";
import { PageState } from "../components/PageState.jsx";
import {
  listAdminUsers,
  listUserMemberships,
  deleteAdminUser,
  assignUserToProject,
  updateMemberRole,
  removeProjectMember,
  grantPlatformPermission,
  revokePlatformPermission,
  listProjects,
  getMe,
} from "../api.js";

const MEMBER_ROLES = ["viewer", "automation_client", "contributor", "project_admin"];

const PLATFORM_PERMS = [
  "manage_users",
  "manage_invitations",
  "manage_roles",
  "manage_providers",
  "view_fleet",
  "create_project",
];

export function AdminUserDetail({ userId, onBack }) {
  const [user, setUser] = useState(null);
  const [memberships, setMemberships] = useState([]);
  const [projects, setProjects] = useState([]);
  const [ownUserId, setOwnUserId] = useState(null);
  const [assignForm, setAssignForm] = useState({ projectId: "", role: "viewer" });
  const [note, setNote] = useState("");
  const [roleNote, setRoleNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null); // null | "forbidden" | "error"

  const reloadUser = () =>
    listAdminUsers()
      .then((users) => {
        setUser(users.find((u) => u.id === userId) ?? null);
        setStatus(null);
      })
      .catch((err) => setStatus(err.message === "forbidden" ? "forbidden" : "error"));

  const reloadMemberships = () =>
    listUserMemberships(userId)
      .then(setMemberships)
      .catch((err) => {
        setStatus(err.message === "forbidden" ? "forbidden" : "error");
        setNote("⚠️ " + err.message);
      });

  const load = () => {
    setLoading(true);
    setStatus(null);
    return Promise.all([reloadUser(), reloadMemberships()]).finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    listProjects()
      .then(setProjects)
      .catch(() => {});
    getMe()
      .then((me) => setOwnUserId(me.id ?? null))
      .catch(() => {});
  }, [userId]);

  const assign = async (e) => {
    e.preventDefault();
    if (!assignForm.projectId) return;
    try {
      await assignUserToProject(userId, parseInt(assignForm.projectId, 10), assignForm.role);
      setNote("Assigned successfully.");
      setAssignForm({ projectId: "", role: "viewer" });
      reloadMemberships();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const changeRole = async (projectId, role) => {
    try {
      await updateMemberRole(projectId, userId, role);
      setRoleNote("Role updated.");
      reloadMemberships();
    } catch (err) {
      setRoleNote("⚠️ " + err.message);
    }
  };

  const remove = async (projectId, projectName) => {
    if (!window.confirm(`Remove this user from "${projectName}"?`)) return;
    try {
      await removeProjectMember(projectId, userId);
      reloadMemberships();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const togglePerm = async (perm) => {
    if (!user) return;
    const has = (user.platform_permissions || []).includes(perm);
    try {
      if (has) {
        await revokePlatformPermission(user.id, perm);
      } else {
        await grantPlatformPermission(user.id, perm);
      }
      reloadUser();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const handleDelete = async () => {
    if (!user) return;
    if (!window.confirm(`Delete ${user.email}? This cannot be undone.`)) return;
    try {
      await deleteAdminUser(user.id);
      onBack();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const isSelf = ownUserId != null && user != null && ownUserId === user.id;

  return (
    <section className="admin-user-detail">
      <button className="link tap-target" onClick={onBack}>
        ← Back to users
      </button>

      <PageState
        forbidden={status === "forbidden"}
        error={status === "error"}
        loading={loading}
        retry={load}
      >
        {user && (
          <>
            <h2>{user.email}</h2>
            {note && <p className="hint">{note}</p>}

            <div className="settings-section">
              <h3>Profile</h3>
              <p>
                {user.display_name || "—"}{" "}
                {user.is_platform_admin && <span className="badge">platform_admin</span>}{" "}
                {!user.is_active && <span className="badge">inactive</span>}
              </p>
            </div>

            <div className="settings-section">
              <h3>Project memberships</h3>
              {roleNote && <p className="hint">{roleNote}</p>}
              {memberships.length === 0 ? (
                <p className="hint">No memberships yet.</p>
              ) : (
                <table className="members-table">
                  <tbody>
                    {memberships.map((m) => (
                      <tr key={m.project_id}>
                        <td>{m.project_name || `#${m.project_id}`}</td>
                        <td>
                          <GatedAction
                            require="manage_users"
                            fallback={<span className="badge">{m.role}</span>}
                          >
                            <select
                              value={m.role}
                              onChange={(e) => changeRole(m.project_id, e.target.value)}
                            >
                              {MEMBER_ROLES.map((r) => (
                                <option key={r} value={r}>
                                  {r}
                                </option>
                              ))}
                            </select>
                          </GatedAction>
                        </td>
                        <td>
                          <GatedAction require="manage_users">
                            <button
                              className="btn-ghost-danger tap-target"
                              onClick={() =>
                                remove(m.project_id, m.project_name || `#${m.project_id}`)
                              }
                            >
                              Remove
                            </button>
                          </GatedAction>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <GatedAction require="manage_users">
                <form onSubmit={assign} className="member-add">
                  <select
                    value={assignForm.projectId}
                    onChange={(e) => setAssignForm((f) => ({ ...f, projectId: e.target.value }))}
                    required
                  >
                    <option value="">Select project…</option>
                    {projects.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  <select
                    value={assignForm.role}
                    onChange={(e) => setAssignForm((f) => ({ ...f, role: e.target.value }))}
                  >
                    {MEMBER_ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                  <button type="submit" className="tap-target">
                    Assign
                  </button>
                </form>
              </GatedAction>
            </div>

            {!user.is_platform_admin && (
              <div className="settings-section">
                <h3>Platform permissions</h3>
                <div className="settings-section-perms">
                  {PLATFORM_PERMS.map((perm) => (
                    <GatedAction key={perm} require="manage_users" fallback={null}>
                      <label className="perm-label tap-target">
                        <input
                          type="checkbox"
                          checked={(user.platform_permissions || []).includes(perm)}
                          onChange={() => togglePerm(perm)}
                        />
                        {perm}
                      </label>
                    </GatedAction>
                  ))}
                </div>
              </div>
            )}

            {!isSelf && (
              <GatedAction require="manage_users">
                <section className="settings-section danger-zone">
                  <h3>Danger zone</h3>
                  <button className="btn btn-danger" onClick={handleDelete}>
                    Delete user
                  </button>
                </section>
              </GatedAction>
            )}
          </>
        )}
      </PageState>
    </section>
  );
}
