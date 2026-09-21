import { useEffect, useState } from "react";
import { GatedAction } from "../context.js";
import {
  listProjectMembers,
  addProjectMember,
  removeProjectMember,
  updateMemberRole,
  listAdminUsers,
} from "../api.js";

const MEMBER_ROLES = ["viewer", "automation_client", "contributor", "project_admin"];

export function MembersPanel({ projectId, setNote, onChanged }) {
  const [members, setMembers] = useState([]);
  const [users, setUsers] = useState([]);
  const [newUserId, setNewUserId] = useState("");
  const [newRole, setNewRole] = useState("viewer");

  const reload = () =>
    listProjectMembers(projectId)
      .then(setMembers)
      .catch((err) => setNote("⚠️ " + err.message));

  useEffect(() => {
    reload();
    listAdminUsers()
      .then(setUsers)
      .catch(() => {});
  }, [projectId]);

  const add = async (e) => {
    e.preventDefault();
    if (!newUserId) return;
    try {
      await addProjectMember(projectId, newUserId, newRole);
      setNewUserId("");
      setNewRole("viewer");
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const remove = async (userId) => {
    try {
      await removeProjectMember(projectId, userId);
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const changeRole = async (userId, role) => {
    try {
      await updateMemberRole(projectId, userId, role);
      reload();
      onChanged?.();
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  return (
    <section className="members-panel">
      <h3>Members</h3>
      {members.length === 0 ? (
        <p className="hint">No members yet.</p>
      ) : (
        <table className="members-table">
          <tbody>
            {members.map((m) => (
              <tr key={m.id}>
                <td>
                  {m.user_display_name || m.user_email || m.user_id}
                  {m.user_not_found && <span className="badge"> (unknown)</span>}
                </td>
                <td>
                  <GatedAction
                    require="manage_members"
                    fallback={<span className="badge">{m.role}</span>}
                  >
                    <select value={m.role} onChange={(e) => changeRole(m.user_id, e.target.value)}>
                      {MEMBER_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </GatedAction>
                </td>
                <td>
                  <GatedAction require="manage_members">
                    <button className="link del" onClick={() => remove(m.user_id)}>
                      ✕
                    </button>
                  </GatedAction>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <GatedAction require="manage_members">
        <form className="member-add" onSubmit={add}>
          <select value={newUserId} onChange={(e) => setNewUserId(e.target.value)} required>
            <option value="">Select user…</option>
            {users.map((u) => (
              <option key={u.id} value={String(u.id)}>
                {u.display_name ? `${u.display_name} (${u.email})` : u.email}
              </option>
            ))}
          </select>
          <select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
            {MEMBER_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button>Add member</button>
        </form>
      </GatedAction>
    </section>
  );
}
