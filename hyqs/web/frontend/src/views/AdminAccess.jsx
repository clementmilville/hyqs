import { ACCESS_TABS, ACCESS_TAB_LABEL, ACCESS_TAB_PERMISSIONS } from "../constants.js";
import { useRole } from "../context.js";
import { PageState } from "../components/PageState.jsx";
import { PermissionsMatrix } from "./PermissionsMatrix.jsx";
import { AdminInvitations } from "./AdminInvitations.jsx";
import { AdminUsers } from "./AdminUsers.jsx";
import { AdminUserDetail } from "./AdminUserDetail.jsx";
import { AdminAudit } from "./AdminAudit.jsx";

export function AdminAccess({ subTab, adminUserId, onNavTab, onNavAdminUser }) {
  const { can } = useRole();
  const activeTab = ACCESS_TABS.includes(subTab) ? subTab : "roles";

  return (
    <div className="admin-access">
      <div className="job-tabs plan-sub-tabs">
        <div className="job-tab-bar">
          {ACCESS_TABS.map((t) => (
            <button
              key={t}
              className={`job-tab-btn tap-target${activeTab === t ? " active" : ""}`}
              onClick={() => onNavTab(t)}
            >
              {ACCESS_TAB_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      {!can(ACCESS_TAB_PERMISSIONS[activeTab]) ? (
        <PageState forbidden>{null}</PageState>
      ) : activeTab === "roles" ? (
        <PermissionsMatrix />
      ) : activeTab === "invitations" ? (
        <AdminInvitations />
      ) : activeTab === "audit" ? (
        <AdminAudit />
      ) : adminUserId != null ? (
        <AdminUserDetail userId={adminUserId} onBack={() => onNavAdminUser(null)} />
      ) : (
        <AdminUsers onOpenUser={onNavAdminUser} />
      )}
    </div>
  );
}
