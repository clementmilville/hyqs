export function IdeationPanel({
  panel,
  confirmDisabled,
  onSelect,
  onEditTitle,
  onEditDesc,
  onRefineChange,
  onRegenerate,
  onConfirm,
  onCancel,
}) {
  return (
    <div className="suggest-panel ideation-panel">
      {panel.options.length === 0 ? (
        <p className="hint">No options returned.</p>
      ) : (
        <div className="suggest-list">
          {panel.options.map((opt, i) => (
            <label
              key={opt.id}
              className={`suggest-item${panel.selectedIdx === i ? " selected" : ""}`}
            >
              <input
                type="radio"
                name="ideation-option"
                checked={panel.selectedIdx === i}
                onChange={() => onSelect(i)}
              />
              <span>
                <span className="suggest-title">{opt.title}</span>
                <span className="suggest-desc">{opt.description}</span>
                <span className="suggest-acceptance">{opt.acceptance}</span>
              </span>
            </label>
          ))}
        </div>
      )}
      {panel.selectedIdx !== null && (
        <div className="ideation-edit">
          <input
            className="ideation-title"
            value={panel.editedTitle}
            onChange={(e) => onEditTitle(e.target.value)}
            placeholder="Feature title…"
          />
          <textarea
            className="ideation-desc-field"
            value={panel.editedDesc}
            onChange={(e) => onEditDesc(e.target.value)}
            placeholder="Feature description…"
            rows={3}
          />
        </div>
      )}
      <div className="suggest-actions">
        <button
          disabled={panel.selectedIdx === null || !panel.editedTitle.trim() || !!confirmDisabled}
          onClick={onConfirm}
        >
          Confirm
        </button>
        <button className="link" onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="ideation-refine">
        <input
          value={panel.refineNote}
          onChange={(e) => onRefineChange(e.target.value)}
          placeholder="Refine further… (e.g. simpler, focus on backend)"
        />
        <button type="button" onClick={onRegenerate} disabled={!panel.refineNote.trim()}>
          Regenerate
        </button>
      </div>
    </div>
  );
}
