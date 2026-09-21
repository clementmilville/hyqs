"""Tests for the startup reconcile sweep that heals legacy split-orphan dependents (S3).

Uses AsyncMock/MagicMock throughout — no Postgres required, matching the
pattern in tests/test_deploy_reconcile.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hyqs.pipeline.runner import PipelineRunner


def _make_runner(store: MagicMock) -> MagicMock:
    store.stuck_at_merge.return_value = []
    store.get_deploying_jobs.return_value = []
    store.reconcile_agent_tasks.return_value = []
    rn = MagicMock()
    rn.store = store
    rn.notify = AsyncMock()
    return rn


def test_reconcile_startup_repoints_legacy_split_orphan_and_emits_event():
    store = MagicMock()
    store.list_split_parents_with_pending_dependents.return_value = [
        {"parent_id": 100, "child_ids": [101, 102]}
    ]
    store.repoint_split_dependents.return_value = [55, 56]
    rn = _make_runner(store)

    asyncio.run(PipelineRunner._reconcile_startup(rn))

    store.repoint_split_dependents.assert_called_once_with(100, [101, 102])
    assert store.add_event.call_count == 2
    for call, dep_id in zip(store.add_event.call_args_list, [55, 56]):
        args, kwargs = call
        assert args[0] == dep_id
        assert args[1] == "plan"
        assert args[2] == "info"
        assert "#100" in kwargs["summary"]
        assert "startup reconcile" in kwargs["summary"]
        assert kwargs["detail"] == {"repointed_from": 100, "repointed_to": [101, 102]}


def test_reconcile_startup_is_noop_when_no_split_orphans():
    store = MagicMock()
    store.list_split_parents_with_pending_dependents.return_value = []
    rn = _make_runner(store)

    asyncio.run(PipelineRunner._reconcile_startup(rn))

    store.repoint_split_dependents.assert_not_called()
    store.add_event.assert_not_called()


def test_reconcile_startup_survives_one_bad_entry_and_processes_rest():
    store = MagicMock()
    store.list_split_parents_with_pending_dependents.return_value = [
        {"parent_id": 1, "child_ids": [2]},
        {"parent_id": 3, "child_ids": [4, 5]},
    ]

    def _repoint(parent_id, child_ids):
        if parent_id == 1:
            raise RuntimeError("boom")
        return [9]

    store.repoint_split_dependents = MagicMock(side_effect=_repoint)
    rn = _make_runner(store)

    asyncio.run(PipelineRunner._reconcile_startup(rn))

    assert store.repoint_split_dependents.call_count == 2
    store.add_event.assert_called_once()
    args, kwargs = store.add_event.call_args
    assert args[0] == 9
    assert kwargs["detail"] == {"repointed_from": 3, "repointed_to": [4, 5]}


def test_reconcile_startup_running_twice_is_idempotent():
    store = MagicMock()
    calls = {"n": 0}

    def _list_orphans():
        calls["n"] += 1
        return [{"parent_id": 7, "child_ids": [8]}] if calls["n"] == 1 else []

    store.list_split_parents_with_pending_dependents = MagicMock(side_effect=_list_orphans)
    store.repoint_split_dependents.return_value = [20]
    rn = _make_runner(store)

    asyncio.run(PipelineRunner._reconcile_startup(rn))
    asyncio.run(PipelineRunner._reconcile_startup(rn))

    assert store.repoint_split_dependents.call_count == 1
    assert store.add_event.call_count == 1
