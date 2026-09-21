"""Generate a concise prose summary of what a job shipped, from the final diff.

Called once at merge time (best-effort — any failure is caught by the caller).
"""

from __future__ import annotations

from pathlib import Path

from . import gitops
from .providers import Role

_DIFF_LIMIT = 30_000  # characters

_SUMMARY_SYS = (
    "You are a concise technical writer. Read the unified diff provided and reply "
    "with 2-4 plain sentences describing what changed and why. "
    "No preamble, no code blocks, no JSON, no bullet points — just prose."
)


async def generate_range_summary(managed_repo: str | Path, range_str: str, *, backend) -> str:
    """Generate a prose summary for an arbitrary git diff range.

    Raises if the diff is empty; callers should catch and degrade to None.
    """
    diff = await gitops.patch(managed_repo, range_str)
    if not diff:
        raise ValueError(f"empty diff for range {range_str!r}")
    if len(diff) > _DIFF_LIMIT:
        diff = diff[:_DIFF_LIMIT] + "\n…(truncated)"
    prompt = f"Summarize what this diff ships:\n\n{diff}"
    run = await backend.run(
        prompt=prompt,
        cwd=str(managed_repo),
        role=Role.REVIEWER,
        append_system=_SUMMARY_SYS,
    )
    return run.text.strip()


async def generate_implementation_summary(managed_repo: str | Path, base: str, *, backend) -> str:
    diff = await gitops.patch(managed_repo, f"{base}^..{base}")
    if len(diff) > _DIFF_LIMIT:
        diff = diff[:_DIFF_LIMIT] + "\n…(truncated)"
    prompt = f"Summarize what this diff ships:\n\n{diff}"
    run = await backend.run(
        prompt=prompt,
        cwd=str(managed_repo),
        role=Role.REVIEWER,
        append_system=_SUMMARY_SYS,
    )
    return run.text.strip()
