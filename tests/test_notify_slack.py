import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from hyqs.pipeline.notify_slack import (
    build_job_url,
    build_webhook_payload,
    send_http_webhook,
    send_slack,
)

_PUBLIC_DNS_RESULT = [
    (2, 1, 6, "", ("104.20.23.154", 443)),
]


def _mock_response(status_code: int, json_body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.is_success = 200 <= status_code < 300
    response.json.return_value = json_body
    response.text = str(json_body)
    return response


def test_send_slack_returns_ts_on_success():
    response = _mock_response(200, {"ok": True, "ts": "1234.5678"})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = asyncio.run(send_slack("xoxb-token", "C123", "hello"))
    assert result == "1234.5678"


def test_send_slack_returns_none_on_non_2xx_status():
    response = _mock_response(500, {"ok": False})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = asyncio.run(send_slack("xoxb-token", "C123", "hello"))
    assert result is None


def test_send_slack_returns_none_when_ok_is_false():
    response = _mock_response(200, {"ok": False, "error": "invalid_auth"})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = asyncio.run(send_slack("xoxb-token", "C123", "hello"))
    assert result is None


def test_send_slack_returns_none_on_network_exception():
    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("boom")),
    ):
        result = asyncio.run(send_slack("xoxb-token", "C123", "hello"))
    assert result is None


def test_send_slack_bot_token_is_required_arg_and_no_env_var_read():
    response = _mock_response(200, {"ok": True, "ts": "1111.2222"})
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = asyncio.run(send_slack(bot_token="xoxb-token", channel_id="C123", text="hi"))
    assert result == "1111.2222"

    source = inspect.getsource(send_slack)
    assert "os.environ" not in source
    assert "getenv" not in source


def test_send_slack_without_thread_ts_omits_it_from_post_body():
    response = _mock_response(200, {"ok": True, "ts": "1234.5678"})
    mock_post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        asyncio.run(send_slack("xoxb-token", "C123", "hello"))
    _, kwargs = mock_post.call_args
    assert "thread_ts" not in kwargs["json"]


def test_send_slack_with_thread_ts_includes_it_in_post_body_and_returns_new_ts():
    response = _mock_response(200, {"ok": True, "ts": "9999.0000"})
    mock_post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient.post", new=mock_post):
        result = asyncio.run(send_slack("xoxb-token", "C123", "hello", thread_ts="1234.5678"))
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["thread_ts"] == "1234.5678"
    assert result == "9999.0000"


def test_build_job_url_matches_frontend_hash_format():
    assert (
        build_job_url("https://hyqs.example.com", 5, 42)
        == "https://hyqs.example.com/#project/5/job/42"
    )


def test_build_job_url_trailing_slash_does_not_double_up():
    assert (
        build_job_url("https://hyqs.example.com/", 5, 42)
        == "https://hyqs.example.com/#project/5/job/42"
    )


def test_build_job_url_returns_none_for_blank_base_url():
    assert build_job_url("", 5, 42) is None
    assert build_job_url("   ", 5, 42) is None


def test_send_http_webhook_posts_json_and_returns_true():
    payload = {"project_id": 7, "job_id": 42, "event_type": "deploy", "summary": "done"}
    response = _mock_response(204, {})
    mock_post = AsyncMock(return_value=response)
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch("httpx.AsyncClient.post", new=mock_post),
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", payload))

    assert result is True
    mock_post.assert_awaited_once_with(
        httpx.URL("https://104.20.23.154/hook"),
        headers={"Host": "example.com"},
        json=payload,
        follow_redirects=False,
        timeout=5.0,
        extensions={"sni_hostname": "example.com"},
    )


def test_send_http_webhook_isolates_timeout_and_non_success():
    payload = {"project_id": 7, "job_id": 42, "event_type": "deploy", "summary": "done"}
    response = _mock_response(500, {"secret": "must not be logged"})
    mock_post = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), response])
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch("httpx.AsyncClient.post", new=mock_post),
    ):
        timeout_result = asyncio.run(send_http_webhook("https://example.com/hook", payload))
        status_result = asyncio.run(send_http_webhook("https://example.com/hook", payload))

    assert timeout_result is False
    assert status_result is False
    expected = (
        httpx.URL("https://104.20.23.154/hook"),
        {
            "headers": {"Host": "example.com"},
            "json": payload,
            "follow_redirects": False,
            "timeout": 5.0,
            "extensions": {"sni_hostname": "example.com"},
        },
    )
    assert [(call.args[0], call.kwargs) for call in mock_post.await_args_list] == [
        expected,
        expected,
    ]


def test_http_webhook_connection_error_returns_false():
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=httpx.ConnectError("connection failed")),
        ) as mock_post,
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", {}))

    assert result is False
    mock_post.assert_awaited_once()


def test_http_webhook_rejects_non_public_destination():
    with (
        patch(
            "asyncio.BaseEventLoop.getaddrinfo",
            new=AsyncMock(return_value=[(2, 1, 6, "", ("169.254.169.254", 80))]),
        ),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        result = asyncio.run(send_http_webhook("http://metadata.local/hook", {}))

    assert result is False
    mock_post.assert_not_awaited()


def test_http_webhook_rejects_each_non_public_address_class():
    addresses = ["127.0.0.1", "10.0.0.1", "169.254.1.1", "192.0.2.1", "169.254.169.254"]
    for address in addresses:
        with (
            patch(
                "asyncio.BaseEventLoop.getaddrinfo",
                new=AsyncMock(return_value=[(2, 1, 6, "", (address, 80))]),
            ),
            patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
        ):
            result = asyncio.run(send_http_webhook("http://destination.example/hook", {}))

        assert result is False, address
        mock_post.assert_not_awaited()


def test_http_webhook_rejects_mixed_public_and_private_dns_results():
    results = [
        (2, 1, 6, "", ("104.20.23.154", 443)),
        (2, 1, 6, "", ("10.0.0.1", 443)),
    ]
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=results)),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", {}))

    assert result is False
    mock_post.assert_not_awaited()


def test_http_webhook_dns_timeout_returns_false_without_posting():
    with (
        patch(
            "asyncio.BaseEventLoop.getaddrinfo",
            new=AsyncMock(side_effect=TimeoutError),
        ),
        patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", {}))

    assert result is False
    mock_post.assert_not_awaited()


def test_http_webhook_does_not_follow_redirect(caplog):
    response = _mock_response(302, {})
    response.headers = {"location": "http://169.254.169.254/latest/meta-data"}
    mock_post = AsyncMock(return_value=response)
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch("httpx.AsyncClient.post", new=mock_post),
        caplog.at_level("WARNING", logger="hyqs.pipeline.notify_slack"),
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", {}))

    assert result is False
    mock_post.assert_awaited_once()
    assert mock_post.await_args.kwargs["follow_redirects"] is False


def test_http_webhook_failure_log_excludes_response_body(caplog):
    response = _mock_response(500, {"secret": "raw-response-secret"})
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        caplog.at_level("WARNING", logger="hyqs.pipeline.notify_slack"),
    ):
        result = asyncio.run(send_http_webhook("https://example.com/hook", {}))

    assert result is False
    assert "raw-response-secret" not in caplog.text
    assert "status 500" in caplog.text


def test_http_webhook_closes_only_internally_created_client():
    response = _mock_response(204, {})
    owned_close = AsyncMock()
    caller_client = MagicMock()
    caller_client.post = AsyncMock(return_value=response)
    caller_client.aclose = AsyncMock()
    with (
        patch("asyncio.BaseEventLoop.getaddrinfo", new=AsyncMock(return_value=_PUBLIC_DNS_RESULT)),
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        patch("httpx.AsyncClient.aclose", new=owned_close),
    ):
        assert asyncio.run(send_http_webhook("https://example.com/hook", {})) is True
        assert (
            asyncio.run(send_http_webhook("https://example.com/hook", {}, client=caller_client))
            is True
        )

    owned_close.assert_awaited_once()
    caller_client.aclose.assert_not_awaited()


def test_http_webhook_rejects_ipv4_embedded_in_ipv6_destination():
    embedded_addresses = [
        "::169.254.169.254",  # deprecated IPv4-compatible ::/96
        "::10.0.0.5",
        "64:ff9b::169.254.169.254",  # NAT64 well-known prefix
        "2002:0a00:0005::",  # 6to4 embeds 10.0.0.5
        "2001:0000:4136:e378:8000:63bf:f5ff:fffa",  # Teredo embeds 10.0.0.5
    ]
    for address in embedded_addresses:
        with (
            patch(
                "asyncio.BaseEventLoop.getaddrinfo",
                new=AsyncMock(return_value=[(10, 1, 6, "", (address, 80, 0, 0))]),
            ),
            patch("httpx.AsyncClient.post", new=AsyncMock()) as mock_post,
        ):
            result = asyncio.run(send_http_webhook("http://metadata.local/hook", {}))

        assert result is False, address
        mock_post.assert_not_awaited()


def test_build_webhook_payload_job_complete_has_only_legacy_keys():
    payload = build_webhook_payload(project_id=7, job_id=42, event_type="job_complete", summary="x")
    assert payload == {
        "project_id": 7,
        "job_id": 42,
        "event_type": "job_complete",
        "summary": "x",
    }
    assert set(payload.keys()) == {"project_id", "job_id", "event_type", "summary"}


def test_build_webhook_payload_deploy_has_only_legacy_keys():
    payload = build_webhook_payload(project_id=7, job_id=42, event_type="deploy", summary="x")
    assert payload == {
        "project_id": 7,
        "job_id": 42,
        "event_type": "deploy",
        "summary": "x",
    }
    assert set(payload.keys()) == {"project_id", "job_id", "event_type", "summary"}


def test_build_webhook_payload_needs_attention_includes_epic_id_and_reason():
    payload = build_webhook_payload(
        project_id=7,
        job_id=42,
        event_type="needs_attention",
        summary="x",
        epic_id=3,
        reason="stuck",
    )
    assert payload == {
        "project_id": 7,
        "job_id": 42,
        "event_type": "needs_attention",
        "summary": "x",
        "epic_id": 3,
        "reason": "stuck",
    }


def test_build_webhook_payload_needs_attention_defaults_epic_id_and_reason_to_none():
    payload = build_webhook_payload(
        project_id=7, job_id=42, event_type="needs_attention", summary="x"
    )
    assert payload["epic_id"] is None
    assert payload["reason"] is None
    assert set(payload.keys()) == {
        "project_id",
        "job_id",
        "event_type",
        "summary",
        "epic_id",
        "reason",
    }


def test_build_webhook_payload_truncates_summary_to_500_chars():
    long_summary = "a" * 600
    for event_type in ("job_complete", "deploy", "needs_attention"):
        payload = build_webhook_payload(
            project_id=7, job_id=42, event_type=event_type, summary=long_summary
        )
        assert len(payload["summary"]) == 500
        assert payload["summary"] == "a" * 500
