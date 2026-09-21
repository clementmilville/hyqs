"""Unit tests for the registry push/sign primitives.

These mock asyncio.create_subprocess_exec so no real docker/cosign daemon is
required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hyqs.pipeline import registry_push

_ENV_VARS = (
    "HYQS_REGISTRY_PUSH_ENABLED",
    "HYQS_REGISTRY_DOMAIN",
    "HYQS_REGISTRY_PUSH_USER",
    "HYQS_REGISTRY_PUSH_PASS",
    "HYQS_COSIGN_KEY_PATH",
    "HYQS_COSIGN_KEY_PASSWORD",
    "HYQS_REGISTRY_PULL_ENABLED",
    "HYQS_REGISTRY_PULL_USER",
    "HYQS_REGISTRY_PULL_PASS",
    "HYQS_COSIGN_PUBLIC_KEY_PATH",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _set_configured_env(monkeypatch, *, enabled: str = "true") -> None:
    monkeypatch.setenv("HYQS_REGISTRY_PUSH_ENABLED", enabled)
    monkeypatch.setenv("HYQS_REGISTRY_DOMAIN", "registry.example.com")
    monkeypatch.setenv("HYQS_REGISTRY_PUSH_USER", "pusher")
    monkeypatch.setenv("HYQS_REGISTRY_PUSH_PASS", "secretpass")
    monkeypatch.setenv("HYQS_COSIGN_KEY_PATH", "/keys/cosign.key")


def _set_pull_configured_env(monkeypatch, *, enabled: str = "true") -> None:
    monkeypatch.setenv("HYQS_REGISTRY_PULL_ENABLED", enabled)
    monkeypatch.setenv("HYQS_REGISTRY_DOMAIN", "registry.example.com")
    monkeypatch.setenv("HYQS_REGISTRY_PULL_USER", "puller")
    monkeypatch.setenv("HYQS_REGISTRY_PULL_PASS", "pullpass")
    monkeypatch.setenv("HYQS_COSIGN_PUBLIC_KEY_PATH", "/keys/cosign.pub")


def _make_proc(stdout_data: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_data, b""))
    return proc


def test_registry_configured_false_by_default():
    assert registry_push.registry_configured() is False


def test_registry_configured_false_when_enabled_flag_unset(monkeypatch):
    monkeypatch.setenv("HYQS_REGISTRY_DOMAIN", "registry.example.com")
    monkeypatch.setenv("HYQS_REGISTRY_PUSH_USER", "pusher")
    monkeypatch.setenv("HYQS_REGISTRY_PUSH_PASS", "secretpass")
    monkeypatch.setenv("HYQS_COSIGN_KEY_PATH", "/keys/cosign.key")
    assert registry_push.registry_configured() is False


def test_registry_configured_false_when_enabled_flag_false(monkeypatch):
    _set_configured_env(monkeypatch, enabled="false")
    assert registry_push.registry_configured() is False


def test_registry_configured_true_when_all_vars_set(monkeypatch):
    _set_configured_env(monkeypatch)
    assert registry_push.registry_configured() is True


def test_build_push_ref_uses_domain(monkeypatch):
    monkeypatch.setenv("HYQS_REGISTRY_DOMAIN", "registry.example.com")
    assert registry_push.build_push_ref("my-proj", "app") == "registry.example.com/my-proj/app"
    assert registry_push.build_push_ref("my-proj") == "registry.example.com/my-proj/app"


def test_push_and_sign_returns_none_and_no_subprocess_calls_when_unconfigured():
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))
    assert result is None
    mock_exec.assert_not_called()


def test_push_image_builds_correct_tag_and_push_argv(monkeypatch):
    _set_configured_env(monkeypatch)
    recorded_calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(list(args))
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok, _output = asyncio.run(
            registry_push.push_image("hyqs-myapp", "registry.example.com/my-proj/app")
        )

    assert ok is True
    tag_call = recorded_calls[0]
    assert tag_call == [
        "docker",
        "tag",
        "hyqs-myapp",
        "registry.example.com/my-proj/app:latest",
    ]
    login_call = recorded_calls[1]
    assert login_call == [
        "docker",
        "login",
        "registry.example.com",
        "-u",
        "pusher",
        "--password-stdin",
    ]
    push_call = recorded_calls[2]
    assert push_call == [
        "docker",
        "push",
        "registry.example.com/my-proj/app:latest",
    ]


def test_push_image_returns_false_on_tag_failure(monkeypatch):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _make_proc(b"boom\n", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok, _output = asyncio.run(
            registry_push.push_image("hyqs-myapp", "registry.example.com/my-proj/app")
        )
    assert ok is False


def test_parse_push_digest_extracts_digest_from_push_output():
    output = "latest: digest: sha256:abc123def456 size: 1022\n"
    assert registry_push._parse_push_digest(output) == "sha256:abc123def456"


def test_parse_push_digest_returns_none_when_no_digest_line():
    assert registry_push._parse_push_digest("no digest here\n") is None


def test_capture_digest_parses_repo_digest():
    async def fake_exec(*args, **kwargs):
        return _make_proc(b"registry.example.com/proj/app@sha256:abcd1234deadbeef\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        digest = asyncio.run(registry_push.capture_digest("registry.example.com/proj/app:latest"))

    assert digest == "sha256:abcd1234deadbeef"


def test_capture_digest_returns_none_on_failure():
    async def fake_exec(*args, **kwargs):
        return _make_proc(b"", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        digest = asyncio.run(registry_push.capture_digest("registry.example.com/proj/app:latest"))

    assert digest is None


def test_capture_digest_returns_none_when_no_at_sign():
    async def fake_exec(*args, **kwargs):
        return _make_proc(b"<none>\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        digest = asyncio.run(registry_push.capture_digest("registry.example.com/proj/app:latest"))

    assert digest is None


def test_sign_digest_invokes_cosign_with_key_and_exact_ref(monkeypatch):
    _set_configured_env(monkeypatch)
    recorded_calls: list[list[str]] = []
    recorded_envs: list[dict] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(list(args))
        recorded_envs.append(kwargs.get("env") or {})
        return _make_proc(b"signed\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.sign_digest("registry.example.com/my-proj/app@sha256:abcd1234")
        )

    assert ok is True
    assert recorded_calls[0] == [
        "cosign",
        "sign",
        "--key",
        "/keys/cosign.key",
        "--yes",
        "registry.example.com/my-proj/app@sha256:abcd1234",
    ]
    assert "COSIGN_PASSWORD" not in recorded_envs[0]


def test_sign_digest_passes_cosign_password_env_when_set(monkeypatch):
    _set_configured_env(monkeypatch)
    monkeypatch.setenv("HYQS_COSIGN_KEY_PASSWORD", "keypass")
    recorded_envs: list[dict] = []

    async def fake_exec(*args, **kwargs):
        recorded_envs.append(kwargs.get("env") or {})
        return _make_proc(b"signed\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.sign_digest("registry.example.com/my-proj/app@sha256:abcd1234")
        )

    assert ok is True
    assert recorded_envs[0]["COSIGN_PASSWORD"] == "keypass"


def test_sign_digest_returns_false_on_cosign_failure(monkeypatch):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _make_proc(b"cosign error\n", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.sign_digest("registry.example.com/my-proj/app@sha256:abcd1234")
        )
    assert ok is False


def test_push_and_sign_success_returns_full_result(monkeypatch):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if cmd[:2] == ["docker", "inspect"]:
            return _make_proc(b"registry.example.com/my-proj/app@sha256:cafef00d\n")
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))

    assert result == {
        "image_ref": "registry.example.com/my-proj/app",
        "image_digest": "sha256:cafef00d",
        "signed": True,
        "output": "pushed and signed",
    }


def test_push_and_sign_uses_push_output_digest_over_repo_digests_inspect(monkeypatch):
    _set_configured_env(monkeypatch)
    inspect_called = False

    async def fake_exec(*args, **kwargs):
        nonlocal inspect_called
        cmd = list(args)
        if cmd[:2] == ["docker", "inspect"]:
            inspect_called = True
            return _make_proc(b"registry.example.com/my-proj/app@sha256:aaaa1111\n")
        if cmd[:2] == ["docker", "push"]:
            return _make_proc(b"latest: digest: sha256:bbbb2222 size: 1022\n")
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))

    assert result["image_digest"] == "sha256:bbbb2222"
    assert inspect_called is False


def test_push_and_sign_falls_back_to_repo_digests_when_push_output_has_no_digest_line(
    monkeypatch,
):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if cmd[:2] == ["docker", "inspect"]:
            return _make_proc(b"registry.example.com/my-proj/app@sha256:cafef00d\n")
        if cmd[:2] == ["docker", "push"]:
            return _make_proc(b"pushed, no digest line here\n")
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))

    assert result["image_digest"] == "sha256:cafef00d"


def test_push_and_sign_returns_failure_dict_on_push_failure(monkeypatch):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _make_proc(b"boom\n", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))

    assert result == {
        "image_ref": "registry.example.com/my-proj/app",
        "image_digest": None,
        "signed": False,
        "output": "docker push failed",
    }


def test_push_and_sign_returns_failure_dict_on_sign_failure(monkeypatch):
    _set_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if cmd[:2] == ["docker", "inspect"]:
            return _make_proc(b"registry.example.com/my-proj/app@sha256:cafef00d\n")
        if cmd[0] == "cosign":
            return _make_proc(b"cosign error\n", returncode=1)
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        result = asyncio.run(registry_push.push_and_sign("hyqs-myapp", "my-proj"))

    assert result == {
        "image_ref": "registry.example.com/my-proj/app",
        "image_digest": None,
        "signed": False,
        "output": "cosign sign failed",
    }


def test_pull_configured_false_by_default():
    assert registry_push.pull_configured() is False


def test_pull_configured_false_when_enabled_flag_unset(monkeypatch):
    monkeypatch.setenv("HYQS_REGISTRY_DOMAIN", "registry.example.com")
    monkeypatch.setenv("HYQS_REGISTRY_PULL_USER", "puller")
    monkeypatch.setenv("HYQS_REGISTRY_PULL_PASS", "pullpass")
    monkeypatch.setenv("HYQS_COSIGN_PUBLIC_KEY_PATH", "/keys/cosign.pub")
    assert registry_push.pull_configured() is False


def test_pull_configured_true_when_all_vars_set(monkeypatch):
    _set_pull_configured_env(monkeypatch)
    assert registry_push.pull_configured() is True


def test_pull_image_by_digest_issues_login_then_pull_with_exact_argv(monkeypatch):
    _set_pull_configured_env(monkeypatch)
    recorded_calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(list(args))
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.pull_image_by_digest(
                "registry.example.com/my-proj/app", "sha256:abcd1234"
            )
        )

    assert ok is True
    login_call = recorded_calls[0]
    assert login_call == [
        "docker",
        "login",
        "registry.example.com",
        "-u",
        "puller",
        "--password-stdin",
    ]
    pull_call = recorded_calls[1]
    assert pull_call == [
        "docker",
        "pull",
        "registry.example.com/my-proj/app@sha256:abcd1234",
    ]


def test_pull_image_by_digest_returns_false_on_login_failure(monkeypatch):
    _set_pull_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        return _make_proc(b"login failed\n", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.pull_image_by_digest(
                "registry.example.com/my-proj/app", "sha256:abcd1234"
            )
        )
    assert ok is False


def test_pull_image_by_digest_returns_false_on_pull_failure(monkeypatch):
    _set_pull_configured_env(monkeypatch)

    async def fake_exec(*args, **kwargs):
        cmd = list(args)
        if cmd[:2] == ["docker", "pull"]:
            return _make_proc(b"pull failed\n", returncode=1)
        return _make_proc(b"ok\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.pull_image_by_digest(
                "registry.example.com/my-proj/app", "sha256:abcd1234"
            )
        )
    assert ok is False


def test_verify_signature_returns_false_without_subprocess_when_key_unset():
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        ok = asyncio.run(
            registry_push.verify_signature("registry.example.com/my-proj/app@sha256:abcd1234")
        )
    assert ok is False
    mock_exec.assert_not_called()


def test_verify_signature_invokes_cosign_verify_with_key_and_exact_ref(monkeypatch):
    monkeypatch.setenv("HYQS_COSIGN_PUBLIC_KEY_PATH", "/keys/cosign.pub")
    recorded_calls: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        recorded_calls.append(list(args))
        return _make_proc(b"Verified OK\n")

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.verify_signature("registry.example.com/my-proj/app@sha256:abcd1234")
        )

    assert ok is True
    assert recorded_calls[0] == [
        "cosign",
        "verify",
        "--key",
        "/keys/cosign.pub",
        "registry.example.com/my-proj/app@sha256:abcd1234",
    ]


def test_verify_signature_returns_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setenv("HYQS_COSIGN_PUBLIC_KEY_PATH", "/keys/cosign.pub")

    async def fake_exec(*args, **kwargs):
        return _make_proc(b"signature verification failed\n", returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        ok = asyncio.run(
            registry_push.verify_signature("registry.example.com/my-proj/app@sha256:abcd1234")
        )
    assert ok is False
