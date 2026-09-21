import { DecisionsList } from "../components/DecisionsList.jsx";

export function DecisionsTab({ projectId }) {
  return (
    <div className="decisions-tab">
      <h2>Decisions</h2>
      <DecisionsList projectId={projectId} />
    </div>
  );
}
