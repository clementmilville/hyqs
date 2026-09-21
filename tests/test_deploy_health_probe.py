"""Tests for deploy.check_public_health (Epic 49 job 2): a single-shot,
non-raising HTTP status probe used by the runtime-status endpoint's `health`
verdict. Unlike verify_http_health this never retries and never raises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from hyqs.pipeline import deploy


def _make_proc(stdout: bytes, returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    return proc


def test_check_public_health_empty_url_is_noop_ok():
    result = asyncio.run(deploy.check_public_health(""))
    assert result == {"http_status": None, "ok": True, "reason": ""}


def test_check_public_health_200_is_ok():
    proc = _make_proc(b"200")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = asyncio.run(deploy.check_public_health("https://example.com"))
    assert result["ok"] is True
    assert result["http_status"] == 200


def test_check_public_health_502_is_not_ok():
    proc = _make_proc(b"502")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        result = asyncio.run(deploy.check_public_health("https://example.com"))
    assert result["ok"] is False
    assert result["http_status"] == 502
    assert "502" in result["reason"]


def test_check_public_health_timeout_returns_ok_false_without_raising():
    async def _raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=_raise_timeout)):
        result = asyncio.run(deploy.check_public_health("https://example.com"))
    assert result["ok"] is False
    assert result["http_status"] is None
    assert result["reason"]


def test_check_public_health_disallowed_url_returns_ok_false_without_raising():
    result = asyncio.run(deploy.check_public_health("http://169.254.169.254/"))
    assert result["ok"] is False
    assert result["http_status"] is None
    assert "blocked address" in result["reason"]
