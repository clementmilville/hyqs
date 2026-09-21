import { useState, useEffect } from "react";
import {
  PanelLeft,
  LayoutDashboard,
  Briefcase,
  ClipboardList,
  ScrollText,
  BarChart2,
  Settings2,
  ShieldCheck,
  FolderOpen,
  Activity,
  Cpu,
  DollarSign,
  Eye,
  LogOut,
  Sun,
  Moon,
  Menu,
  PlusCircle,
} from "lucide-react";
import { GatedAction, useRole } from "../context.js";
import { ProjectSwitcher } from "./ProjectSwitcher.jsx";
import { canSeeAdminNav, canSeeAdminTab } from "../constants.js";
import { useTheme } from "../theme.jsx";

function useIsTablet() {
  const [isTablet, setIsTablet] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(min-width: 600px)").matches
  );
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 600px)");
    const handler = (e) => setIsTablet(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isTablet;
}

function rovingKeyDown(e, sectionSelector) {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  e.preventDefault();
  const section = e.currentTarget.closest(sectionSelector);
  const buttons = Array.from(section?.querySelectorAll("button") ?? []);
  const idx = buttons.indexOf(e.currentTarget);
  if (idx === -1) return;
  const next =
    e.key === "ArrowDown"
      ? buttons[(idx + 1) % buttons.length]
      : buttons[(idx - 1 + buttons.length) % buttons.length];
  next?.focus();
}

export function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("hyqs_sidebar_collapsed") === "true"
  );
  const toggle = () =>
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem("hyqs_sidebar_collapsed", String(next));
      return next;
    });
  return [collapsed, toggle];
}

export const WS_NAV = [
  { id: "dashboard", label: "Overview", Icon: LayoutDashboard },
  { id: "plan", label: "Plan", Icon: ClipboardList },
  { id: "work", label: "Work", Icon: Briefcase },
  { id: "history", label: "History", Icon: ScrollText },
  { id: "analytics", label: "Analytics", Icon: BarChart2 },
];

export const ADMIN_NAV = [
  { id: "command-center", label: "Command Center", Icon: Activity },
  { id: "projects", label: "Projects", Icon: FolderOpen },
  { id: "new-project", label: "New Project", Icon: PlusCircle },
  { id: "providers", label: "Providers", Icon: Cpu },
  { id: "access", label: "Access", Icon: ShieldCheck },
  { id: "usage-cost", label: "Usage & Cost", Icon: DollarSign },
  { id: "visitor-analytics", label: "Visitor analytics", Icon: Eye },
];

export function Sidebar({
  context,
  wsTab,
  adminTab,
  projects,
  currentProjectId,
  onNavWorkspace,
  onNavAdmin,
  onSwitch,
  onSignOut,
  collapsed,
  onToggle,
  mobileOpen,
  onMobileClose,
}) {
  const { role, can, authError, retryAuth } = useRole();
  const { theme, toggleTheme } = useTheme();
  const isTablet = useIsTablet();
  const hasAnyAdminPerm = canSeeAdminNav(can);

  const sidebarClass = ["sidebar", collapsed ? "collapsed" : "", mobileOpen ? "mobile-open" : ""]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <nav className={sidebarClass}>
        <div className="sidebar-brand">
          {collapsed ? (
            <span className="sidebar-brand-icon">H</span>
          ) : (
            <span className="sidebar-brand-text">Hyqs</span>
          )}
          <button className="sidebar-collapse-btn" onClick={onToggle} title="Toggle sidebar">
            <PanelLeft
              size={18}
              style={{
                transform: collapsed ? "rotate(180deg)" : "none",
                transition: "transform 200ms",
              }}
            />
          </button>
        </div>

        {context === "workspace" && (
          <div className="sidebar-project-switcher">
            {collapsed ? (
              <button className="sidebar-nav-item" onClick={onToggle} title="Switch project">
                <FolderOpen size={18} />
              </button>
            ) : (
              <ProjectSwitcher
                projects={projects}
                currentId={currentProjectId}
                onSwitch={onSwitch}
              />
            )}
          </div>
        )}

        <div className="sidebar-scroll-area">
          <div className="sidebar-nav">
            {WS_NAV.map(({ id, label, Icon }) => (
              <button
                key={id}
                className={`sidebar-nav-item${
                  context === "workspace" && wsTab === id ? " active" : ""
                }`}
                aria-current={context === "workspace" && wsTab === id ? "page" : undefined}
                onClick={() => onNavWorkspace(id)}
                onKeyDown={(e) => rovingKeyDown(e, ".sidebar-nav")}
                title={label}
              >
                <Icon size={18} />
                {!collapsed && <span>{label}</span>}
              </button>
            ))}
            <GatedAction require="edit_project">
              <button
                className={`sidebar-nav-item${
                  context === "workspace" && wsTab === "settings" ? " active" : ""
                }`}
                aria-current={context === "workspace" && wsTab === "settings" ? "page" : undefined}
                onClick={() => onNavWorkspace("settings")}
                onKeyDown={(e) => rovingKeyDown(e, ".sidebar-nav")}
                title="Settings"
              >
                <Settings2 size={18} />
                {!collapsed && <span>Settings</span>}
              </button>
            </GatedAction>
          </div>

          {hasAnyAdminPerm && (
            <div className="sidebar-admin-section">
              <hr className="sidebar-divider" />
              {!collapsed && <div className="sidebar-section-label">Admin</div>}
              {ADMIN_NAV.map(({ id, label, Icon }) => {
                if (!canSeeAdminTab(id, can)) return null;
                return (
                  <button
                    key={id}
                    className={`sidebar-nav-item${
                      context === "admin" && adminTab === id ? " active" : ""
                    }`}
                    aria-current={context === "admin" && adminTab === id ? "page" : undefined}
                    onClick={() => onNavAdmin(id)}
                    onKeyDown={(e) => rovingKeyDown(e, ".sidebar-admin-section")}
                    title={label}
                  >
                    <Icon size={18} />
                    {!collapsed && <span>{label}</span>}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="sidebar-footer">
          {authError && (
            <div className="sidebar-auth-error">
              {!collapsed && <span>Auth failed</span>}
              <button
                className="sidebar-auth-retry-btn"
                onClick={retryAuth}
                title="Retry authentication"
              >
                {collapsed ? "!" : "Retry"}
              </button>
            </div>
          )}
          {!collapsed && (
            <div className="sidebar-kbd-hint">
              <kbd>⌘K</kbd>
              <span>Command palette</span>
            </div>
          )}
          <button
            className="sidebar-nav-item"
            onClick={toggleTheme}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
            {!collapsed && <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>}
          </button>
          {!collapsed && role && <div className="sidebar-role-label">{role}</div>}
          <button className="sidebar-nav-item" onClick={onSignOut} title="Sign out">
            <LogOut size={18} />
            {!collapsed && <span>Sign out</span>}
          </button>
        </div>
      </nav>
      {mobileOpen && isTablet && <div className="sidebar-overlay" onClick={onMobileClose} />}
    </>
  );
}

export function SidebarHamburger({ onClick }) {
  return (
    <button className="sidebar-hamburger" onClick={onClick} title="Open menu">
      <Menu size={20} />
    </button>
  );
}
