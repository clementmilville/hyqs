export default function IntakeBlocks({ blocks }) {
  if (!blocks) return null;
  const { summary, body, action_items } = blocks;
  if (!summary && !body && (!action_items || action_items.length === 0)) return null;

  return (
    <div className="intake-blocks">
      {summary && (
        <div className="intake-block-summary">
          <p>{summary}</p>
        </div>
      )}
      {body && (
        <div className="intake-block-body">
          <p>{body}</p>
        </div>
      )}
      {action_items && action_items.length > 0 && (
        <ul className="intake-block-actions">
          {action_items.map((item, i) => (
            <li key={i}>{item.trim()}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
