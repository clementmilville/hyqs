import { useEffect, useState } from "react";
import { KNOWN_PERMISSIONS, ROLE_NAMES } from "../constants.js";
import { useRole } from "../context.js";
import { fetchRolePermissions, patchRolePermission } from "../api.js";
import { PageState } from "../components/PageState.jsx";
import { usePageData } from "../hooks/usePageData.js";

const STICKY_HEADER_STYLE = {
  position: "sticky",
  top: 0,
  background: "var(--color-surface)",
  zIndex: "var(--z-dropdown)",
};
const STICKY_COL_STYLE = { position: "sticky", left: 0, background: "var(--color-surface)" };
const STICKY_CORNER_STYLE = {
  ...STICKY_HEADER_STYLE,
  ...STICKY_COL_STYLE,
  zIndex: "calc(var(--z-dropdown) + 1)",
};

export function PermissionsMatrix() {
  const { setPermissions } = useRole();
  const { data, loading, forbidden, error, retry } = usePageData(fetchRolePermissions);
  const [matrix, setMatrix] = useState(null);
  const [toggleError, setToggleError] = useState("");

  useEffect(() => {
    if (!data) return;
    const sets = {};
    for (const [r, perms] of Object.entries(data)) {
      sets[r] = new Set(perms);
    }
    setMatrix(sets);
  }, [data]);

  async function toggle(role, permission, checked) {
    const prev = matrix;
    setMatrix((m) => {
      const next = { ...m };
      next[role] = new Set(m[role]);
      if (checked) next[role].add(permission);
      else next[role].delete(permission);
      return next;
    });
    try {
      await patchRolePermission(role, permission, checked);
      setPermissions((m) => {
        const next = { ...m };
        next[role] = new Set(m[role] || []);
        if (checked) next[role].add(permission);
        else next[role].delete(permission);
        return next;
      });
      setToggleError("");
    } catch (err) {
      setMatrix(prev);
      setToggleError(err.message || "Failed to update permission");
    }
  }

  return (
    <div className="fleet">
      <h3 className="fleet-section-head">Role Permissions</h3>
      {toggleError && <p className="note">⚠️ {toggleError}</p>}
      <p className="hint">
        Toggle which permissions each role has. The admin-area permissions (
        <code>manage_users</code>, <code>manage_invitations</code>, <code>manage_roles</code>,{" "}
        <code>manage_providers</code>, <code>view_fleet</code>) are permanently locked for{" "}
        <code>platform_admin</code>.
      </p>
      <PageState forbidden={forbidden} error={error} loading={loading || !matrix} retry={retry}>
        {matrix && (
          <div className="table-scroll">
            <table className="sup-table">
              <thead>
                <tr>
                  <th style={STICKY_CORNER_STYLE}>Permission</th>
                  {ROLE_NAMES.map((r) => (
                    <th key={r} style={STICKY_HEADER_STYLE}>
                      {r.replace(/_/g, " ")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {KNOWN_PERMISSIONS.map((perm) => (
                  <tr key={perm}>
                    <td style={STICKY_COL_STYLE}>
                      <code>{perm}</code>
                    </td>
                    {ROLE_NAMES.map((role) => {
                      const locked =
                        role === "platform_admin" &&
                        [
                          "manage_users",
                          "manage_invitations",
                          "manage_roles",
                          "manage_providers",
                          "view_fleet",
                        ].includes(perm);
                      const checked = locked ? true : (matrix[role]?.has(perm) ?? false);
                      return (
                        <td key={role}>
                          <label className="tap-target">
                            <input
                              type="checkbox"
                              checked={checked}
                              disabled={locked}
                              onChange={(e) => toggle(role, perm, e.target.checked)}
                            />
                          </label>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PageState>
    </div>
  );
}
