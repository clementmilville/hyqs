import { useEffect, useState } from "react";
import { updateProject } from "../api.js";

const DEFAULT_FIX_BUDGET = 3;

export function FixBudgetPanel({ project, setNote, onChanged }) {
  const [value, setValue] = useState(project.max_fix_attempts ?? "");

  useEffect(() => {
    setValue(project.max_fix_attempts ?? "");
  }, [project.id, project.max_fix_attempts]);

  const save = async () => {
    const num = value === "" ? null : parseInt(value, 10);
    if (num !== null && (isNaN(num) || num < 1)) {
      setNote("⚠️ Fix budget must be a positive integer, or clear it to use the default");
      return;
    }
    try {
      await updateProject(project.id, { max_fix_attempts: num });
      onChanged();
      setNote(num === null ? "Fix budget reset to default." : `Fix budget set to ${num}.`);
    } catch (err) {
      setNote("⚠️ " + err.message);
    }
  };

  const effective = project.max_fix_attempts ?? DEFAULT_FIX_BUDGET;

  return (
    <section className="fix-budget">
      <div className="fix-budget-row">
        <span className="fix-budget-label">🔁 Fix budget</span>
        <input
          type="number"
          min="1"
          value={value}
          placeholder={`default (${DEFAULT_FIX_BUDGET})`}
          onChange={(e) => setValue(e.target.value)}
          onBlur={save}
          className="fix-budget-input"
        />
        <span className="hint">max self-heal rounds per job (effective: {effective})</span>
      </div>
    </section>
  );
}
