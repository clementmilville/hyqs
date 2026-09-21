"""Deterministic rate-limit handling — NO AI.

When an AI stage hits the Anthropic usage/rate limit, the supervisor has to
notice and pause *without* itself depending on AI — because AI is exactly what
is unavailable at that moment. So a limit surfaces as a plain typed exception
that the runner catches with pure Python; see the project rule in
``testing.py`` (prefer deterministic automation over AI).
"""

from __future__ import annotations

import time

MAX_PROVIDER_PAUSE_SECONDS = 6 * 60 * 60


class ProviderUnavailable(Exception):
    """A provider rejected work because a confirmed capacity limit was reached.

    This exception is exclusively for rate, usage, quota, or overload limits.
    Authentication, billing, invalid-request, tool, and generic backend failures
    must retain their ordinary error semantics. It carries which ``provider`` hit
    the limit and the unix timestamp when the window resets, when reported.
    The runner reverts the job to PENDING at its current stage and idles that
    provider until then — no work, no AI, no lost progress.
    """

    def __init__(
        self, provider: str = "claude", resets_at: int | None = None, detail: str = ""
    ) -> None:
        super().__init__(detail or f"{provider} rate/usage limit reached")
        self.provider = provider
        self.resets_at = resets_at
        self.detail = detail


# Back-compat alias: the gate used to be Anthropic-only.
RateLimited = ProviderUnavailable


def pause_until(resets_at: int | None, fallback_seconds: int, now: float | None = None) -> float:
    """Return a bounded provider pause from reset metadata or the fallback."""
    now = time.time() if now is None else now
    maximum = now + MAX_PROVIDER_PAUSE_SECONDS
    if resets_at and resets_at > now:
        return min(float(resets_at) + 5.0, maximum)
    return min(now + float(fallback_seconds), maximum)
