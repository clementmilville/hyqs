from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(slots=True)
class IntakeSession:
    id: int
    session_id: str
    created_by: str
    draft_spec: dict
    messages: list[dict]
    status: str
    project_id: int | None
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(row) -> "IntakeSession":
        draft_spec = row["draft_spec"]
        if isinstance(draft_spec, str):
            draft_spec = json.loads(draft_spec)
        elif draft_spec is None:
            draft_spec = {}

        messages = row["messages"]
        if isinstance(messages, str):
            messages = json.loads(messages)
        elif messages is None:
            messages = []

        return IntakeSession(
            id=row["id"],
            session_id=row["session_id"],
            created_by=row["created_by"],
            draft_spec=draft_spec,
            messages=messages,
            status=row["status"],
            project_id=row["project_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
