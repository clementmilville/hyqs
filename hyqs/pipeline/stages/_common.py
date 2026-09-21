"""Shared helpers used by more than one stage handler (moved out of runner)."""
from __future__ import annotations

import re

_PR_CONFLICT_RE = re.compile(r"merge conflict|not mergeable", re.IGNORECASE)
_NON_FAST_FORWARD_RE = re.compile(r"non-fast-forward|\[rejected\]|fetch first", re.IGNORECASE)


def _is_non_fast_forward(stderr: str) -> bool:
    """True if a `git push` failed because the remote branch diverged from local."""
    return bool(_NON_FAST_FORWARD_RE.search(stderr or ''))
