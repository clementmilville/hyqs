"""Local deployer host-config + state, persisted as JSON under a data dir.

Pure file I/O only — no JobStore, no DB, no network. This is the state a
standalone deploy host keeps about itself: which host it is, its enrollment
key path, and a rolling record of what it last claimed/deployed.
"""

from __future__ import annotations

import json
from pathlib import Path


def _deployer_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "deployer"


def _host_config_path(data_dir: str | Path) -> Path:
    return _deployer_dir(data_dir) / "host_config.json"


def _state_path(data_dir: str | Path) -> Path:
    return _deployer_dir(data_dir) / "state.json"


def write_host_config(data_dir: str | Path, host_name: str, private_key_path: str) -> None:
    """Persist this host's name and private-key path to ``host_config.json``."""
    path = _host_config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"host_name": host_name, "private_key_path": private_key_path})
    )


def read_host_config(data_dir: str | Path) -> dict | None:
    """The host config written by ``write_host_config``, or None if absent."""
    path = _host_config_path(data_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _read_state_file(data_dir: str | Path) -> dict:
    path = _state_path(data_dir)
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def _write_state_file(data_dir: str | Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _append_environment_served(state: dict, project_id: int, environment_id: int) -> None:
    entry = {"project_id": project_id, "environment_id": environment_id}
    served = state.setdefault("environments_served", [])
    if entry not in served:
        served.append(entry)


def record_claim(data_dir: str | Path, *, job_id: int, project_id: int, environment_id: int) -> None:
    """Merge a claim event into ``state.json`` without clobbering other fields."""
    state = _read_state_file(data_dir)
    state["last_claim"] = {
        "job_id": job_id,
        "project_id": project_id,
        "environment_id": environment_id,
    }
    _append_environment_served(state, project_id, environment_id)
    _write_state_file(data_dir, state)


def record_deploy(
    data_dir: str | Path,
    *,
    job_id: int,
    project_id: int,
    environment_id: int,
    deployed_commit: str,
) -> None:
    """Merge a deploy event into ``state.json`` without clobbering other fields."""
    state = _read_state_file(data_dir)
    state["last_deploy"] = {
        "job_id": job_id,
        "project_id": project_id,
        "environment_id": environment_id,
        "deployed_commit": deployed_commit,
    }
    _append_environment_served(state, project_id, environment_id)
    _write_state_file(data_dir, state)


def record_apply(
    data_dir: str | Path,
    *,
    promotion_id: int,
    project_id: int,
    environment_id: int,
    release_id: int,
    applied_by: str,
    deployed_commit: str = "",
) -> None:
    """Merge an apply event into ``state.json`` without clobbering other fields."""
    state = _read_state_file(data_dir)
    state["last_apply"] = {
        "promotion_id": promotion_id,
        "project_id": project_id,
        "environment_id": environment_id,
        "release_id": release_id,
        "applied_by": applied_by,
        "deployed_commit": deployed_commit,
    }
    _append_environment_served(state, project_id, environment_id)
    _write_state_file(data_dir, state)


def record_decline(
    data_dir: str | Path,
    *,
    promotion_id: int,
    project_id: int,
    environment_id: int,
    reason: str,
    applied_by: str,
) -> None:
    """Merge a decline event into ``state.json`` without clobbering other fields."""
    state = _read_state_file(data_dir)
    state["last_decline"] = {
        "promotion_id": promotion_id,
        "project_id": project_id,
        "environment_id": environment_id,
        "reason": reason,
        "applied_by": applied_by,
    }
    _write_state_file(data_dir, state)


def read_state(data_dir: str | Path) -> dict:
    """The current deployer state, or ``{}`` if nothing has been recorded yet."""
    return _read_state_file(data_dir)
