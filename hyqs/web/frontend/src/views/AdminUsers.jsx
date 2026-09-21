import { listAdminUsers } from "../api.js";
import { PageState } from "../components/PageState.jsx";
import { usePageData } from "../hooks/usePageData.js";

export function AdminUsers({ onOpenUser }) {
  const { data, loading, forbidden, error, retry } = usePageData(listAdminUsers);
  const users = data || [];

  return (
    <section className="admin-users">
      <h2>Users</h2>
      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        <div className="table-scroll">
          <table className="perf-jobs-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Display name</th>
                <th>Role</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>{u.email}</td>
                  <td>{u.display_name || "—"}</td>
                  <td>{u.is_platform_admin && <span className="badge">platform_admin</span>}</td>
                  <td>
                    <button className="link tap-target" onClick={() => onOpenUser(u.id)}>
                      Manage
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </PageState>
    </section>
  );
}
