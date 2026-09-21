export function ThinkingIndicator({ visible }) {
  if (!visible) return null;
  return (
    <div className="thinking-indicator">
      <span className="thinking-dot" aria-hidden="true" />
      <span>Working…</span>
    </div>
  );
}
