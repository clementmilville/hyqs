from __future__ import annotations

from hyqs.pipeline.deployer_state import (
    read_host_config,
    read_state,
    record_claim,
    record_deploy,
    write_host_config,
)


def test_write_then_read_host_config_round_trips(tmp_path):
    write_host_config(tmp_path, "host-1", "/etc/hyqs/host-1.pem")

    assert read_host_config(tmp_path) == {
        "host_name": "host-1",
        "private_key_path": "/etc/hyqs/host-1.pem",
    }


def test_read_host_config_returns_none_when_missing(tmp_path):
    assert read_host_config(tmp_path) is None


def test_read_state_returns_empty_dict_when_missing(tmp_path):
    assert read_state(tmp_path) == {}


def test_record_claim_sets_last_claim_and_environments_served(tmp_path):
    record_claim(tmp_path, job_id=1, project_id=10, environment_id=100)

    state = read_state(tmp_path)
    assert state["last_claim"] == {"job_id": 1, "project_id": 10, "environment_id": 100}
    assert state["environments_served"] == [{"project_id": 10, "environment_id": 100}]


def test_record_deploy_sets_last_deploy_and_environments_served(tmp_path):
    record_deploy(
        tmp_path, job_id=2, project_id=10, environment_id=100, deployed_commit="abc123"
    )

    state = read_state(tmp_path)
    assert state["last_deploy"] == {
        "job_id": 2,
        "project_id": 10,
        "environment_id": 100,
        "deployed_commit": "abc123",
    }
    assert state["environments_served"] == [{"project_id": 10, "environment_id": 100}]


def test_record_claim_then_record_deploy_merges_without_clobbering(tmp_path):
    record_claim(tmp_path, job_id=1, project_id=10, environment_id=100)
    record_deploy(
        tmp_path, job_id=1, project_id=10, environment_id=100, deployed_commit="abc123"
    )

    state = read_state(tmp_path)
    assert state["last_claim"] == {"job_id": 1, "project_id": 10, "environment_id": 100}
    assert state["last_deploy"] == {
        "job_id": 1,
        "project_id": 10,
        "environment_id": 100,
        "deployed_commit": "abc123",
    }


def test_environments_served_accumulates_deduped_list(tmp_path):
    record_claim(tmp_path, job_id=1, project_id=10, environment_id=100)
    record_deploy(
        tmp_path, job_id=1, project_id=10, environment_id=100, deployed_commit="abc123"
    )
    record_claim(tmp_path, job_id=2, project_id=20, environment_id=200)
    record_claim(tmp_path, job_id=3, project_id=10, environment_id=100)

    state = read_state(tmp_path)
    assert state["environments_served"] == [
        {"project_id": 10, "environment_id": 100},
        {"project_id": 20, "environment_id": 200},
    ]
