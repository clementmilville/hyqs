import React from "react";

export const RoleContext = React.createContext({
  role: "viewer",
  can: () => false,
  setPermissions: () => {},
  authLoading: true,
  authError: false,
  retryAuth: () => {},
});

export function useRole() {
  return React.useContext(RoleContext);
}

export const ProjectContext = React.createContext({ project: null });

export function GatedAction({ require: action, fallback = null, children }) {
  const { can, authLoading } = useRole();
  if (authLoading) return null;
  return can(action) ? children : fallback;
}
