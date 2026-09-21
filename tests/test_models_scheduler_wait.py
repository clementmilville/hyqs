"""Focused tests for SchedulerWait/SchedulerWaitReason and lock-owner helpers."""

import pytest

from hyqs.pipeline.models import (
    SchedulerWait,
    SchedulerWaitReason,
    lock_owner_id,
    parse_lock_owner_id,
)


def test_scheduler_wait_reason_members_have_snake_case_values():
    assert SchedulerWaitReason.PROJECT_SLOT_OCCUPIED.value == "project_slot_occupied"
    assert SchedulerWaitReason.FILE_OVERLAP.value == "file_overlap"
    assert SchedulerWaitReason.SCHEMA_LOCK.value == "schema_lock"
    assert SchedulerWaitReason.MERGE_LOCK.value == "merge_lock"
    assert SchedulerWaitReason.PROVIDER_CAPACITY.value == "provider_capacity"


def test_scheduler_wait_to_dict_stringifies_reason():
    wait = SchedulerWait(
        reason=SchedulerWaitReason.FILE_OVERLAP,
        summary="job 5 overlaps with job 9 on 2 files",
        blocking_job_ids=[9],
        conflicting_paths=["hyqs/pipeline/models.py"],
    )

    assert wait.to_dict() == {
        "reason": "file_overlap",
        "summary": "job 5 overlaps with job 9 on 2 files",
        "blocking_job_ids": [9],
        "conflicting_paths": ["hyqs/pipeline/models.py"],
    }


def test_lock_owner_id_round_trips_with_parse_lock_owner_id():
    assert lock_owner_id(7) == "job-7"
    assert parse_lock_owner_id(lock_owner_id(7)) == 7


@pytest.mark.parametrize("owner", ["job-", "job-abc", "agent-7", "job-7x", ""])
def test_parse_lock_owner_id_returns_none_for_malformed_or_foreign_owner(owner):
    assert parse_lock_owner_id(owner) is None
