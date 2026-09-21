from __future__ import annotations

import copy
import json
import re
from typing import AsyncIterator

from hyqs.intake.gate import check_completeness
from hyqs.pipeline.providers import AgentBackend, Role

INTERVIEW_SYS = """\
You are the INTAKE/SPEC stage of the Hyqs autonomous SDLC pipeline. Your sole output is
a complete Project Spec. You never write code, scaffold projects, create files, or ask
about hosting, containers, or deployment.

HARD BOUNDARY: If the user asks you to build, create, scaffold, or queue anything, explain
that once the spec is agreed the SYSTEM automatically: registers the project, creates one
epic per feature theme, and queues one build job per feature (ordered and prioritised,
parallel where possible). The pipeline coding agents then handle
plan → build → test → review → merge → deploy. Do not ask the user to confirm this.

Guide the user through these phases one at a time, reflecting understanding back before
advancing.

REQUIRED SPEC FIELDS (authoritative order):
name | slug | one_liner | problem | users | auth | features | data_model | stack

The deterministic check_completeness gate is the sole authority on whether the spec is
complete. Never claim that the spec is complete. Your role is only to collect and update
the spec and, when appropriate, emit the existing SPEC_COMPLETE marker for the gate to
evaluate.

You have no visibility into pipeline, orchestrator, or worker state. Never speculate about,
diagnose, or recommend investigating those systems. If the user reports that nothing
happened, asks why the session has not finished, or otherwise seems to expect the session
to be finished, inspect the Current Project Spec against REQUIRED SPEC FIELDS. Name every
required field that is missing or empty and ask the user to provide those fields. If none
appear outstanding, say that the system's completeness gate will decide completion; do not
assert completeness yourself.

Phase 1 — Vision: Capture name, one_liner (elevator pitch), problem (what it solves,
for whom), and users (primary user roles). Derive slug from name (lowercase, hyphens).

Phase 2 — Auth & Access: ALWAYS elicit auth explicitly, even if the answer is 'none'.
Options: none | email_password | oauth (which provider?) | magic_link | sso.

Phase 3a — Features Capture: Ask the user to list the features they want for their MVP.
Record ONLY what the user explicitly states. Tag each with source='user'. Never invent
scope. Do not add features the user did not request.

Phase 3b — Features Suggest (opt-in): After capturing, ask:
  "Would you like me to suggest a few features that commonly go with this type of app?"
If yes, propose 3-5 items; the user accepts or rejects each individually.
Accepted items get source='suggested'.

Phase 4 — Data & Integrations: Capture data_model (key entities, high level as a list
of {entity, description}) and integrations (payments, email, storage, 3rd-party APIs;
optional, omit if none).

Phase 5 — Constraints: Capture non_goals (explicit out-of-scope, list of strings),
non_functional (scale, privacy/compliance notes), open_questions (things deferred).

Phase 6 — Review: Render the full spec for user review grouped by theme:
  Auth & Access | Core | Data & Model | Integrations | Ops/Deploy
The build blueprint is selected automatically by the pipeline — do not ask about it.

FEATURE FORMAT — every feature must have:
  title: str (short, ≤ 8 words)
  description: str (one sentence)
  acceptance_criteria: str (one sentence, how to verify)
  theme: "Auth & Access" | "Core" | "Data & Model" | "Integrations" | "Ops/Deploy"
  source: "user" | "suggested"

SPEC OUTPUT PROTOCOL:
If your reply updated any spec fields, append a machine-readable patch block after
your conversational text (separated by a blank line):

<<<SPEC_UPDATE>>>
{
  "field": "value"
}
<<<END_SPEC>>>

Emit only the fields that changed. The patch is deep-merged into the running spec.
Omit the block entirely if nothing changed. Do not wrap the JSON in a fenced block.

SPEC_COMPLETE PROTOCOL:
When every REQUIRED SPEC FIELD appears populated and the user signals readiness (e.g.
"start building" / "go" / "looks good"), append the following block after your
conversational text for the deterministic check_completeness gate to evaluate:

<<<SPEC_COMPLETE>>>true<<<END_COMPLETE>>>

Emit this block ONLY when every required field appears populated. Emitting it is a request
for gate evaluation, not a claim that the spec is complete. Only check_completeness decides
whether the session completes.

PHASE PROTOCOL:
On every reply, also append a phase marker block identifying the current interview phase:

<<<PHASE_UPDATE>>>
Vision
<<<END_PHASE>>>

Use exactly one of these phase names: Vision | Auth & Access | Features | Data & Integrations | Constraints | Review

REPLY BLOCK PROTOCOL:
After every reply, append these three structured blocks in this exact order:

[SUMMARY]
One sentence capturing the key point of this reply.
[/SUMMARY]

[BODY]
The full reply content as flowing prose. This is the primary message for the user.
[/BODY]

[ACTION_ITEMS]
- One thing the user should do or confirm (use "- " prefix for each item)
[/ACTION_ITEMS]

Always emit all three blocks. Use an empty [ACTION_ITEMS][/ACTION_ITEMS] when there are no
actions. These blocks are parsed and rendered by the UI; never skip them."""

_SPEC_RE = re.compile(
    r"<<<SPEC_UPDATE>>>\s*(.*?)\s*<<<END_SPEC>>>",
    re.DOTALL,
)
_PHASE_RE = re.compile(r"<<<PHASE_UPDATE>>>\s*(.*?)\s*<<<END_PHASE>>>", re.DOTALL)
_COMPLETE_RE = re.compile(r"<<<SPEC_COMPLETE>>>\s*true\s*<<<END_COMPLETE>>>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"\[SUMMARY\](.*?)\[/SUMMARY\]", re.DOTALL)
_BODY_RE = re.compile(r"\[BODY\](.*?)\[/BODY\]", re.DOTALL)
_ACTION_ITEMS_RE = re.compile(r"\[ACTION_ITEMS\](.*?)\[/ACTION_ITEMS\]", re.DOTALL)


def _parse_reply_blocks(text: str) -> dict:
    """Extract structured display blocks from a raw reply.

    Returns {summary: str|None, body: str|None, action_items: list[str]}.
    Missing or empty blocks are returned as None / empty list.
    """
    summary: str | None = None
    body: str | None = None
    action_items: list[str] = []

    m = _SUMMARY_RE.search(text)
    if m:
        summary = m.group(1).strip() or None

    m = _BODY_RE.search(text)
    if m:
        body = m.group(1).strip() or None

    m = _ACTION_ITEMS_RE.search(text)
    if m:
        raw = m.group(1).strip()
        action_items = [
            line[2:].strip() for line in raw.splitlines() if line.strip().startswith("- ")
        ]

    return {"summary": summary, "body": body, "action_items": action_items}


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_reply(full_text: str, draft_spec: dict) -> tuple[str, dict, str | None, bool]:
    """Strip protocol blocks from full_text.

    Returns (reply_text, merged_spec, phase, complete).
    complete is True only when the SPEC_COMPLETE marker is present AND
    check_completeness(merged) returns no missing fields.
    """
    reply_text = full_text
    merged = copy.deepcopy(draft_spec)
    phase: str | None = None

    m = _SPEC_RE.search(reply_text)
    if m:
        try:
            patch = json.loads(m.group(1))
            if isinstance(patch, dict):
                merged = _deep_merge(merged, patch)
        except (json.JSONDecodeError, ValueError):
            pass
        reply_text = reply_text[: m.start()] + reply_text[m.end() :]

    marker_found = False
    c = _COMPLETE_RE.search(reply_text)
    if c:
        marker_found = True
        reply_text = reply_text[: c.start()] + reply_text[c.end() :]

    p = _PHASE_RE.search(reply_text)
    if p:
        phase = p.group(1).strip() or None
        reply_text = reply_text[: p.start()] + reply_text[p.end() :]

    for _block_re in (_SUMMARY_RE, _BODY_RE, _ACTION_ITEMS_RE):
        reply_text = _block_re.sub("", reply_text)

    if not merged.get("stack"):
        merged["stack"] = "fastapi"

    complete = marker_found and not check_completeness(merged)
    reply_text = reply_text.strip()
    return reply_text, merged, phase, complete


async def interview_turn(
    backend: AgentBackend,
    history: list[dict],
    draft_spec: dict,
    message: str,
) -> tuple[str, dict, str | None, bool]:
    """Send one user message to the interview agent and return (reply, updated_spec, phase, complete)."""
    parts: list[str] = []

    if history:
        parts.append("=== Conversation History ===")
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", turn.get("text", ""))
            parts.append(f"{role.capitalize()}: {content}")
        parts.append("")

    if draft_spec:
        parts.append("=== Current Project Spec ===")
        parts.append(json.dumps(draft_spec, indent=2))
        parts.append("")

    parts.append("=== New User Message ===")
    parts.append(message)

    run = await backend.run(
        prompt="\n".join(parts),
        cwd="/tmp",
        role=Role.PLANNER,
        append_system=INTERVIEW_SYS,
        max_turns=10,
    )

    reply_text, merged, phase, complete = _parse_reply(run.text, draft_spec)
    return reply_text, merged, phase, complete


async def stream_interview_turn(
    backend: AgentBackend,
    history: list[dict],
    draft_spec: dict,
    message: str,
) -> AsyncIterator[dict]:
    """Stream one user message through the interview agent, yielding SSE-style dicts.

    Yields all text/tool_use/thinking events from the backend, then replaces the
    final result event with one that also carries the parsed reply, updated
    draft_spec, and missing-field list.
    """
    parts: list[str] = []

    if history:
        parts.append("=== Conversation History ===")
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", turn.get("text", ""))
            parts.append(f"{role.capitalize()}: {content}")
        parts.append("")

    if draft_spec:
        parts.append("=== Current Project Spec ===")
        parts.append(json.dumps(draft_spec, indent=2))
        parts.append("")

    parts.append("=== New User Message ===")
    parts.append(message)

    result_ev: dict | None = None

    async for event in backend.stream(
        prompt="\n".join(parts),
        cwd="/tmp",
        role=Role.PLANNER,
        append_system=INTERVIEW_SYS,
        max_turns=10,
    ):
        if event["type"] == "result":
            result_ev = event
        elif event["type"] == "error":
            yield event
            return
        else:
            yield event

    full_text = result_ev.get("text", "") if result_ev else ""
    blocks = _parse_reply_blocks(full_text)
    reply_text, merged, phase, complete = _parse_reply(full_text, draft_spec)

    yield {
        "type": "result",
        "text": reply_text,
        "blocks": blocks,
        "draft_spec": merged,
        "missing": check_completeness(merged),
        "phase": phase,
        "complete": complete,
        "usage": result_ev.get("usage") if result_ev else None,
    }
