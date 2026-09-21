export function ProjectSwitcher({ projects, currentId, onSwitch }) {
  if (projects.length === 0) return null;
  return (
    <select
      value={currentId ?? ""}
      onChange={(e) => onSwitch(Number(e.target.value))}
      className="project-switcher"
      title="Switch project"
    >
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}
