// `icon` accepts either a string emoji (back-compat) or a lucide-react icon
// component (e.g. `icon={ClipboardList}`).
export function EmptyState({ icon, title, hint }) {
  const isEmoji = typeof icon === "string";
  const Icon = isEmoji ? null : icon;
  return (
    <div className="empty-state">
      {isEmoji && <span className="empty-icon">{icon}</span>}
      {Icon && <Icon className="empty-icon" size={32} aria-hidden="true" />}
      <p className="empty-title">{title}</p>
      {hint && <p className="empty-hint">{hint}</p>}
    </div>
  );
}
