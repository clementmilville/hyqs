"""Tests for the enroll/status/apply/decline subcommands and --deployer flag
(jobs #1781 and #1788)."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hyqs.pipeline import deployer_state
from hyqs.pipeline.__main__ import (
    _parse_args,
    _run_apply,
    _run_decline,
    _run_enroll,
    _run_status,
    main,
)
from hyqs.pipeline.models import PromotionKind, PromotionState


def _unique_name() -> str:
    return f"host-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


def test_parse_args_enroll_requires_name():
    with pytest.raises(SystemExit):
        _parse_args(["enroll"])


def test_parse_args_enroll_with_name():
    args = _parse_args(["enroll", "--name", "host-a"])
    assert args.command == "enroll"
    assert args.name == "host-a"


def test_parse_args_status():
    args = _parse_args(["status"])
    assert args.command == "status"


def test_parse_args_no_command_defaults_to_none():
    args = _parse_args([])
    assert args.command is None
    assert args.deployer is False


def test_parse_args_deployer_flag():
    args = _parse_args(["--deployer"])
    assert args.deployer is True


def test_parse_args_apply_defaults_operator_to_none():
    args = _parse_args(["apply"])
    assert args.command == "apply"
    assert args.operator is None


def test_parse_args_apply_with_operator():
    args = _parse_args(["apply", "--operator", "alice"])
    assert args.operator == "alice"


def test_parse_args_decline_requires_reason():
    with pytest.raises(SystemExit):
        _parse_args(["decline"])


def test_parse_args_decline_with_reason():
    args = _parse_args(["decline", "--reason", "bad build"])
    assert args.command == "decline"
    assert args.reason == "bad build"
    assert args.operator is None


# ---------------------------------------------------------------------------
# _run_enroll
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, data_dir, db_url):
        self.data_dir = str(data_dir)
        self.db_url = db_url


def test_run_enroll_registers_host_and_writes_local_files(monkeypatch, store, tmp_path):
    name = _unique_name()
    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env",
        lambda: _FakeConfig(tmp_path, store._dsn),
    )

    exit_code = _run_enroll(name)

    assert exit_code == 0
    host = store.get_host_by_name(name)
    assert host is not None

    key_path = tmp_path / "deployer" / "host_key.pem"
    assert key_path.is_file()
    assert "PRIVATE KEY" in key_path.read_text()

    host_config = deployer_state.read_host_config(tmp_path)
    assert host_config == {"host_name": name, "private_key_path": str(key_path)}


def test_run_enroll_reusing_persisted_key_is_idempotent(monkeypatch, store, tmp_path):
    """``_run_enroll`` always mints a fresh keypair, so re-running the CLI
    verbatim would (correctly, per ``enroll_host``'s contract) refuse to
    silently rotate the host's key. What must stay idempotent is re-enrolling
    with the SAME persisted key — e.g. a deploy host restarting and replaying
    its already-written ``host_key.pem``."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_private_key,
    )

    from hyqs.pipeline import hosts

    name = _unique_name()
    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env",
        lambda: _FakeConfig(tmp_path, store._dsn),
    )

    assert _run_enroll(name) == 0
    host_config = deployer_state.read_host_config(tmp_path)
    private_pem = Path(host_config["private_key_path"]).read_text()
    public_pem = (
        load_pem_private_key(private_pem.encode(), password=None)
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    first = hosts.enroll_host(store, name, public_pem)
    second = hosts.enroll_host(store, name, public_pem)

    assert first.id == second.id


# ---------------------------------------------------------------------------
# _run_status
# ---------------------------------------------------------------------------


class _NoDbConfig:
    """A config whose ``db_url`` raises if ever touched — proves _run_status
    never constructs a JobStore or otherwise reaches for the database."""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)

    @property
    def db_url(self):
        raise AssertionError("_run_status must never touch config.db_url")


def test_run_status_reports_not_enrolled_when_no_host_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env", lambda: _NoDbConfig(tmp_path)
    )

    assert _run_status() == 1


def test_run_status_prints_identity_without_touching_db(monkeypatch, tmp_path):
    deployer_state.write_host_config(tmp_path, "host-a", str(tmp_path / "host_key.pem"))
    deployer_state.record_claim(tmp_path, job_id=1, project_id=2, environment_id=3)

    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env", lambda: _NoDbConfig(tmp_path)
    )

    assert _run_status() == 0


# ---------------------------------------------------------------------------
# main() dispatch: --deployer requires HYQS_HOST_NAME
# ---------------------------------------------------------------------------


def test_main_deployer_flag_exits_when_host_name_unset(monkeypatch):
    class _UnpinnedConfig:
        pipeline_host_name = ""

    def _boom(*args, **kwargs):
        raise AssertionError("should not start runner when unpinned")

    monkeypatch.setattr("hyqs.pipeline.__main__.Config.from_env", lambda: _UnpinnedConfig())
    monkeypatch.setattr("hyqs.pipeline.__main__.asyncio.run", _boom)
    monkeypatch.setattr("sys.argv", ["hyqs-pipeline", "--deployer"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


def test_main_deployer_flag_proceeds_when_host_name_set(monkeypatch):
    class _PinnedConfig:
        pipeline_host_name = "host-a"

    captured = {}

    def _fake_run_asyncio(coro):
        captured["ran"] = True
        coro.close()

    monkeypatch.setattr("hyqs.pipeline.__main__.Config.from_env", lambda: _PinnedConfig())
    monkeypatch.setattr("hyqs.pipeline.__main__.asyncio.run", _fake_run_asyncio)
    monkeypatch.setattr("hyqs.pipeline.__main__.set_db_actor", lambda actor: None, raising=False)
    monkeypatch.setattr("sys.argv", ["hyqs-pipeline", "--deployer"])

    main()

    assert captured.get("ran") is True


# ---------------------------------------------------------------------------
# _run_apply / _run_decline
# ---------------------------------------------------------------------------


@pytest.fixture
def apply_setup(store, tmp_path, monkeypatch):
    """An enrolled host pinned to an environment with an available offer."""
    project = store.create_project(
        f"Deployer Apply Project {uuid.uuid4()}", f"/tmp/test-deployer-{uuid.uuid4()}"
    )
    host = store.create_host(_unique_name())
    env = store.create_environment(project.id, "client-prod", "staging")
    store.set_environment_host(env.id, host.id)
    release = store.create_release(
        project.id, "abc123", "registry.example.com/proj/app", "sha256:deadbeef"
    )
    offer = store.offer_release(project.id, release.id, env.id)

    deployer_state.write_host_config(tmp_path, host.name, str(tmp_path / "host_key.pem"))
    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env",
        lambda: _FakeConfig(tmp_path, store._dsn),
    )

    yield store, tmp_path, host, env, release, offer
    store.delete_project(project.id)


def test_run_apply_no_available_offer_returns_1_and_touches_no_promotion(
    monkeypatch, store, tmp_path
):
    project = store.create_project(
        f"Deployer No Offer Project {uuid.uuid4()}", f"/tmp/test-deployer-{uuid.uuid4()}"
    )
    host = store.create_host(_unique_name())
    env = store.create_environment(project.id, "client-prod", "staging")
    store.set_environment_host(env.id, host.id)
    deployer_state.write_host_config(tmp_path, host.name, str(tmp_path / "host_key.pem"))
    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env",
        lambda: _FakeConfig(tmp_path, store._dsn),
    )

    try:
        with patch(
            "hyqs.pipeline.__main__.docker_deploy.pull_deploy", new_callable=AsyncMock
        ) as mock_pull:
            exit_code = _run_apply("alice")
    finally:
        store.delete_project(project.id)

    assert exit_code == 1
    mock_pull.assert_not_called()


def test_run_apply_success_advances_promotion_and_writes_receipt(monkeypatch, apply_setup):
    store, tmp_path, host, env, release, offer = apply_setup

    with patch(
        "hyqs.pipeline.__main__.docker_deploy.pull_deploy",
        new_callable=AsyncMock,
        return_value={"deployed": True, "verified": True, "output": ""},
    ) as mock_pull:
        exit_code = _run_apply("alice")

    assert exit_code == 0
    mock_pull.assert_awaited_once()
    updated = store.get_promotion(offer.id)
    assert updated.state == PromotionState.DEPLOYED
    assert updated.applied_by == "alice"
    assert store.get_environment(env.id).current_release_id == release.id

    state = deployer_state.read_state(tmp_path)
    assert state["last_apply"]["promotion_id"] == offer.id
    assert state["last_apply"]["release_id"] == release.id
    assert state["last_apply"]["applied_by"] == "alice"


def test_run_apply_not_deployed_transitions_failed_and_leaves_current_release(
    monkeypatch, apply_setup
):
    store, tmp_path, host, env, release, offer = apply_setup

    with patch(
        "hyqs.pipeline.__main__.docker_deploy.pull_deploy",
        new_callable=AsyncMock,
        return_value={"deployed": False, "verified": False, "output": "pull failed"},
    ):
        exit_code = _run_apply("alice")

    assert exit_code == 1
    assert store.get_promotion(offer.id).state == PromotionState.FAILED
    assert store.get_environment(env.id).current_release_id is None


def test_run_apply_missing_secret_behaves_like_generic_failure(monkeypatch, apply_setup):
    store, tmp_path, host, env, release, offer = apply_setup

    with patch(
        "hyqs.pipeline.__main__.docker_deploy.pull_deploy",
        new_callable=AsyncMock,
        return_value={
            "deployed": False,
            "verified": False,
            "output": "missing required secrets: DATABASE_URL",
        },
    ):
        exit_code = _run_apply("alice")

    assert exit_code == 1
    assert store.get_promotion(offer.id).state == PromotionState.FAILED
    assert store.get_environment(env.id).current_release_id is None


def test_run_apply_not_enrolled_returns_1_without_touching_db(monkeypatch, tmp_path):
    class _NoDbConfig:
        def __init__(self, data_dir):
            self.data_dir = str(data_dir)

        @property
        def db_url(self):
            raise AssertionError("_run_apply must not touch the DB when not enrolled")

    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env", lambda: _NoDbConfig(tmp_path)
    )

    assert _run_apply("alice") == 1


def test_run_decline_transitions_offer_and_records_reason(monkeypatch, apply_setup):
    store, tmp_path, host, env, release, offer = apply_setup

    exit_code = _run_decline("bad build", "alice")

    assert exit_code == 0
    updated = store.get_promotion(offer.id)
    assert updated.state == PromotionState.DECLINED
    assert updated.kind == PromotionKind.OFFER

    state = deployer_state.read_state(tmp_path)
    assert state["last_decline"]["promotion_id"] == offer.id
    assert state["last_decline"]["reason"] == "bad build"
    assert state["last_decline"]["applied_by"] == "alice"


def test_run_decline_not_enrolled_returns_1_without_touching_db(monkeypatch, tmp_path):
    class _NoDbConfig:
        def __init__(self, data_dir):
            self.data_dir = str(data_dir)

        @property
        def db_url(self):
            raise AssertionError("_run_decline must not touch the DB when not enrolled")

    monkeypatch.setattr(
        "hyqs.pipeline.__main__.Config.from_env", lambda: _NoDbConfig(tmp_path)
    )

    assert _run_decline("bad build", "alice") == 1
