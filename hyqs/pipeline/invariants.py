"""Invariant ratchet — deterministic, per-project regression guards. NO AI.

Projects may drop executable checks under ``.hyqs/invariants/`` that encode
past review/incident decisions as permanent, diff-scoped guards. The TEST
stage runs them after the test suite; a violation routes into the same FIX
loop as a failing test. Projects without the directory are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hyqs.invariants")

CHECK_TIMEOUT = 60  # seconds per check
TOTAL_BUDGET = 300  # seconds across all checks in one run

INVARIANTS_DIR = ".hyqs/invariants"

README_TEMPLATE = """\
# Invariant checks

Executable, deterministic checks in this directory run after the TEST stage
on every job's diff. They encode past review/incident decisions as permanent
regression guards — once a lesson is learned, it should never need to be
re-learned by an AI reviewer.

## Contract

- Any executable `*.sh` or `*.py` file here is a check. Checks run in
  filename order.
- **Inputs**: the job's changed files (repo-relative paths), one per line,
  are written to the check's **stdin**. The same list is also available as
  the newline-joined `HYQS_CHANGED_FILES` env var. `HYQS_BASE_REF` carries
  the merge-base ref, so a check can diff content itself
  (e.g. `git diff --unified=0 "$HYQS_BASE_REF...HEAD"`).
- **Exit codes**:
  - `0` — pass
  - `1` — violation. stdout + stderr are captured verbatim and shown to the
    FIX agent, prefixed with the check's filename.
  - `78` — not applicable / skip (e.g. no relevant files changed).
  - any other exit code, a crash, or a timeout — treated as **skip** with a
    logged warning. A broken check must never fail an unrelated job.
- **Diff-scoping is mandatory**: a check must judge only the provided
  changed files, never the whole repo. Judging pre-existing code would trap
  every future job that merely touches a file with old debt.

## Budget

Each check gets 60s; the whole run is capped at 5 minutes. Checks that don't
run because the budget ran out are recorded as skipped.

## Generated inventory blocks

CONVENTIONS.md holds two kinds of content: curated NORMS (hand-written rules) and
generated INVENTORY (what tokens/components/endpoints exist today — goes stale if
hand-maintained). Content between ``<!-- hyqs:generated:begin -->`` and
``<!-- hyqs:generated:end -->`` in CONVENTIONS.md is INVENTORY: a check here may
regenerate it wholesale and exit 1 (with the regenerated block on stdout) when it
drifts from the source of truth. Never mark hand-written NORMS text this way.

Use ``hyqs.pipeline.conventions_block.replace_generated_block`` (idempotent
marker-block replacement) to build such a check; see
``hyqs.pipeline.conventions_block.CSS_TOKEN_INVARIANT_TEMPLATE`` for a
copy-paste starting point for CSS-token projects.
"""


@dataclass
class InvariantResult:
    name: str
    status: str  # "pass" | "fail" | "skip"
    output: str = ""


def discover(worktree: str | Path) -> list[Path]:
    """Return the project's invariant checks (``*.sh``/``*.py``), sorted by filename.

    Missing directory -> empty list (zero overhead for projects that don't opt in).
    """
    checks_dir = Path(worktree) / INVARIANTS_DIR
    if not checks_dir.is_dir():
        return []
    checks = [p for p in checks_dir.iterdir() if p.is_file() and p.suffix in (".sh", ".py")]
    return sorted(checks, key=lambda p: p.name)


def _interpreter(path: Path) -> list[str]:
    return ["python3", str(path)] if path.suffix == ".py" else ["bash", str(path)]


async def _run_check(
    path: Path,
    worktree: str | Path,
    base_ref: str,
    changed_files: list[str],
    timeout: float,
) -> InvariantResult:
    stdin_text = "\n".join(changed_files)
    env = {
        "HYQS_CHANGED_FILES": "\n".join(changed_files),
        "HYQS_BASE_REF": base_ref,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *_interpreter(path),
            cwd=str(worktree),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        log.warning("invariants: could not launch check %s: %s", path.name, exc)
        return InvariantResult(name=path.name, status="skip", output=str(exc))

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_text.encode()), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        log.warning("invariants: check %s timed out after %ss", path.name, timeout)
        return InvariantResult(name=path.name, status="skip", output="timed out")

    output = (stdout_b.decode(errors="replace") + stderr_b.decode(errors="replace")).strip()
    rc = proc.returncode
    if rc == 0:
        return InvariantResult(name=path.name, status="pass", output=output)
    if rc == 1:
        return InvariantResult(name=path.name, status="fail", output=output)
    if rc == 78:
        return InvariantResult(name=path.name, status="skip", output=output)
    log.warning("invariants: check %s exited %s (treated as skip)", path.name, rc)
    return InvariantResult(name=path.name, status="skip", output=output)


async def run_invariants(
    worktree: str | Path,
    base_ref: str,
    changed_files: list[str],
) -> list[InvariantResult]:
    """Run every discovered check against the job's diff. Never raises.

    Runs sequentially in filename order so the total budget can be enforced
    across the whole batch; checks left once the budget is spent are
    recorded as skipped rather than run.
    """
    try:
        checks = discover(worktree)
    except OSError as exc:
        log.warning("invariants: discovery failed: %s", exc)
        return []

    results: list[InvariantResult] = []
    remaining_budget = TOTAL_BUDGET
    for i, path in enumerate(checks):
        if remaining_budget <= 0:
            log.warning(
                "invariants: total budget exhausted; skipping remaining %d check(s)",
                len(checks) - i,
            )
            results.extend(
                InvariantResult(name=p.name, status="skip", output="total budget exhausted")
                for p in checks[i:]
            )
            break
        per_check_timeout = min(CHECK_TIMEOUT, remaining_budget)
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        try:
            result = await _run_check(path, worktree, base_ref, changed_files, per_check_timeout)
        except Exception as exc:  # noqa: BLE001 - a broken check must never fail the job
            log.warning("invariants: check %s raised unexpected exception: %s", path.name, exc)
            result = InvariantResult(name=path.name, status="skip", output=str(exc))
        results.append(result)
        remaining_budget -= loop.time() - t0
    return results


def ensure_readme(project_path: str | Path) -> None:
    """Create ``.hyqs/invariants/README.md`` documenting the check contract, if absent."""
    checks_dir = Path(project_path) / INVARIANTS_DIR
    readme = checks_dir / "README.md"
    if readme.exists():
        return
    checks_dir.mkdir(parents=True, exist_ok=True)
    readme.write_text(README_TEMPLATE)
