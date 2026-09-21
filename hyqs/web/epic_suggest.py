"""Non-blocking AI helper that suggests the best-fit epic for a job idea.

Always wrapped in try/except — returns {} on any error so callers degrade
gracefully without blocking the job-creation flow.
"""

from __future__ import annotations

import json
import logging

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_RESULT_MARKER = "<<<RESULT_JSON>>>"
_END_MARKER = "<<<END_RESULT>>>"


def suggest_best_epic(idea: str, epics: list[dict]) -> dict:
    """Return {epic_id: int}, {proposed_name: str}, or {} on error/empty.

    Calls claude-haiku-4-5 with a short prompt.  Never raises.
    """
    try:
        if anthropic is None or not epics:
            return {}

        epic_lines = "\n".join(
            f"- id={e['id']}, name={e['name']!r}"
            + (f", description={e['description']!r}" if e.get("description") else "")
            for e in epics
        )
        prompt = (
            f"A developer wants to create a job with this idea:\n{idea!r}\n\n"
            f"Available epics:\n{epic_lines}\n\n"
            "If an existing epic clearly fits, return a result block containing "
            f'{{"epic_id": <int>}}. '
            "If none fit well, return a result block containing "
            f'{{"proposed_name": "<short epic name>"}}. '
            f"Use the sentinel format exactly:\n"
            f"{_RESULT_MARKER}\n"
            "{{...}}\n"
            f"{_END_MARKER}"
        )

        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text

        s = text.find(_RESULT_MARKER)
        e = text.find(_END_MARKER)
        if s != -1 and e != -1 and e > s:
            block = text[s + len(_RESULT_MARKER) : e].strip()
            data = json.loads(block)
            if "epic_id" in data:
                eid = int(data["epic_id"])
                if any(ep["id"] == eid for ep in epics):
                    return {"epic_id": eid}
                return {}
            if "proposed_name" in data and data["proposed_name"]:
                return {"proposed_name": str(data["proposed_name"])}
        return {}
    except Exception:
        log.debug("suggest_best_epic failed", exc_info=True)
        return {}
