"""Pure host-config verification for activation-followup jobs.

An activation-followup job (``source_meta.kind == "activation-followup"``)
exists to confirm a merged change was switched on in the live deployment —
typically an env var set in the repo-root ``.env``. That file is gitignored,
so it can never appear in a job's isolated git worktree; checking it there
always fails, permanently, regardless of the host's real state (job #4337).

These functions read no I/O themselves — callers pass in the host's resolved
environment (``os.environ`` in the parent pipeline process, populated by
``hyqs.config``'s ``load_dotenv()`` at import time) so the check is testable
without mocking the environment.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping

_ENV_VAR_RE = re.compile(r"\bHYQS_[A-Z0-9_]+\b")


class ActivationVerification(str, enum.Enum):
    satisfied = "satisfied"
    unsatisfied = "unsatisfied"
    unobservable = "unobservable"


def extract_activation_env_var(activation_location: str, expected_live_effect: str) -> str | None:
    """The ``HYQS_*`` env var name this activation-followup names, if any.

    Searches ``activation_location`` first, falling back to
    ``expected_live_effect`` when the location names none.
    """
    match = _ENV_VAR_RE.search(activation_location)
    if match:
        return match.group(0)
    match = _ENV_VAR_RE.search(expected_live_effect)
    return match.group(0) if match else None


def verify_env_activation(
    var_name: str | None, host_env: Mapping[str, str]
) -> tuple[ActivationVerification, str]:
    """Classify a host-config activation as satisfied/unsatisfied/unobservable.

    ``var_name=None`` means the follow-up named no checkable variable — that
    is unobservable, not a pass or a fail. Otherwise a non-empty value in
    ``host_env`` is satisfied; a missing or blank value is unsatisfied.
    """
    if var_name is None:
        return (
            ActivationVerification.unobservable,
            "activation follow-up names no HYQS_* environment variable to check",
        )
    value = host_env.get(var_name, "").strip()
    if value:
        return (
            ActivationVerification.satisfied,
            f"{var_name} is set on the host (process-resolved, non-empty)",
        )
    return (
        ActivationVerification.unsatisfied,
        f"{var_name} is unset or empty in the host's resolved configuration",
    )
