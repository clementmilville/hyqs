import { useEffect, useRef, useState } from "react";
import { LogOut, Menu, Moon, RefreshCw, Settings2, Sun } from "lucide-react";
import { WS_NAV, ADMIN_NAV } from "./Sidebar.jsx";
import { ProjectSwitcher } from "./ProjectSwitcher.jsx";
import { canSeeAdminNav, canSeeAdminTab } from "../constants.js";
import { useRole } from "../context.js";
import { useTheme } from "../theme.jsx";

export function BottomTabBar({
  context,
  active,
  can,
  projects = [],
  currentProjectId,
  onNavAdmin,
  onNavigate,
  onSwitch,
  onSignOut,
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const { theme, toggleTheme } = useTheme();
  const { authError, retryAuth } = useRole();
  const hasAdminNav = canSeeAdminNav(can);
  const menuIsActive = (context === "workspace" && active === "settings") || context === "admin";

  useEffect(() => {
    if (!menuOpen) return undefined;
    menuRef.current?.querySelector("button, select")?.focus();
    const handleKeyDown = (event) => {
      if (event.key !== "Escape") return;
      setMenuOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [menuOpen]);

  const closeAnd =
    (callback) =>
    (...args) => {
      setMenuOpen(false);
      callback?.(...args);
    };

  return (
    <>
      {menuOpen && (
        <>
          <button
            className="mobile-more-scrim"
            aria-label="Close navigation menu"
            onClick={() => setMenuOpen(false)}
          />
          <div
            id="mobile-more-menu"
            className="mobile-more-menu"
            role="dialog"
            aria-label="More navigation"
            ref={menuRef}
          >
            {projects.length > 0 && (
              <div className="mobile-more-group">
                <label className="mobile-more-label">
                  Project
                  <ProjectSwitcher
                    projects={projects}
                    currentId={currentProjectId}
                    onSwitch={closeAnd(onSwitch)}
                  />
                </label>
              </div>
            )}

            {can("edit_project") && (
              <div className="mobile-more-group">
                <button
                  className="mobile-more-item"
                  aria-current={
                    context === "workspace" && active === "settings" ? "page" : undefined
                  }
                  onClick={closeAnd(() => onNavigate("settings"))}
                >
                  <Settings2 className="mobile-nav-icon" />
                  <span>Settings</span>
                </button>
              </div>
            )}

            {hasAdminNav && (
              <div className="mobile-more-group">
                <div className="mobile-more-label">Admin</div>
                {ADMIN_NAV.filter(({ id }) => canSeeAdminTab(id, can)).map(
                  ({ id, label, Icon }) => (
                    <button
                      key={id}
                      className="mobile-more-item"
                      aria-current={context === "admin" && active === id ? "page" : undefined}
                      onClick={closeAnd(() => onNavAdmin(id))}
                    >
                      <Icon className="mobile-nav-icon" />
                      <span>{label}</span>
                    </button>
                  )
                )}
              </div>
            )}

            <div className="mobile-more-group">
              <div className="mobile-more-label">Account</div>
              {authError && (
                <button className="mobile-more-item" onClick={closeAnd(retryAuth)}>
                  <RefreshCw className="mobile-nav-icon" />
                  <span>Retry authentication</span>
                </button>
              )}
              <button className="mobile-more-item" onClick={closeAnd(toggleTheme)}>
                {theme === "dark" ? (
                  <Sun className="mobile-nav-icon" />
                ) : (
                  <Moon className="mobile-nav-icon" />
                )}
                <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
              </button>
              <button className="mobile-more-item" onClick={closeAnd(onSignOut)}>
                <LogOut className="mobile-nav-icon" />
                <span>Sign out</span>
              </button>
            </div>
          </div>
        </>
      )}

      <nav className="bottom-tab-bar" aria-label="Mobile navigation">
        {WS_NAV.map(({ id, label, Icon }) => (
          <button
            key={id}
            className="tab-item"
            aria-current={context === "workspace" && active === id ? "page" : undefined}
            onClick={() => onNavigate(id)}
            title={label}
          >
            <Icon className="mobile-nav-icon" />
            <span>{label}</span>
          </button>
        ))}
        <button
          ref={triggerRef}
          className="tab-item"
          aria-label="More navigation"
          aria-expanded={menuOpen}
          aria-controls="mobile-more-menu"
          aria-current={menuIsActive ? "page" : undefined}
          onClick={() => setMenuOpen((open) => !open)}
          title="More"
        >
          <Menu className="mobile-nav-icon" />
          <span>More</span>
        </button>
      </nav>
    </>
  );
}
