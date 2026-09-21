"""Send a chat message via Slack's chat.postMessage API."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

import httpx

logger = logging.getLogger(__name__)

_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_HTTP_TIMEOUT = 5.0

# Prefixes that embed an IPv4 address inside an IPv6 address. ipaddress.is_global
# does not unwrap these, so e.g. 64:ff9b::169.254.169.254 (NAT64) or
# ::169.254.169.254 (deprecated IPv4-compatible) read as globally routable even
# though the embedded address is link-local/private.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPAT_PREFIX = ipaddress.ip_network("::/96")


def _embedded_ipv4_addresses(ip: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, ...]:
    """Return IPv4 addresses carried by common IPv6 transition formats."""
    embedded: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    elif ip in _NAT64_PREFIX or ip in _IPV4_COMPAT_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.sixtofour is not None:
        embedded.append(ip.sixtofour)
    if ip.teredo is not None:
        embedded.extend(ip.teredo)
    return tuple(embedded)


def _is_global(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """ip.is_global, but IPv4-in-IPv6 embeddings are unwrapped and re-checked."""
    if not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return all(embedded.is_global for embedded in _embedded_ipv4_addresses(ip))
    return True


def build_job_url(base_url: str, project_id: int, job_id: int) -> str | None:
    """The web console URL for a job's detail page, or None if base_url is unset.

    Builds the same hash route as the frontend's ``hashForJob`` (App.jsx):
    ``#project/<projectId>/job/<jobId>``. Kept as the single place this format
    is constructed on the backend.
    """
    base_url = base_url.strip()
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/#project/{project_id}/job/{job_id}"


def build_webhook_payload(
    *,
    project_id: int,
    job_id: int | None,
    event_type: str,
    summary: str,
    epic_id: int | None = None,
    reason: str | None = None,
) -> dict:
    """The JSON body sent to a registered HTTP webhook.

    Kept as the single place this format is constructed on the backend.
    ``epic_id``/``reason`` are only included for ``needs_attention`` events;
    ``job_complete``/``deploy`` keep the legacy 4-key shape.
    """
    payload = {
        "project_id": project_id,
        "job_id": job_id,
        "event_type": event_type,
        "summary": summary[:500],
    }
    if event_type == "needs_attention":
        payload["epic_id"] = epic_id
        payload["reason"] = reason
    return payload


async def send_slack(
    bot_token: str, channel_id: str, text: str, thread_ts: str | None = None
) -> str | None:
    payload = {"channel": channel_id, "text": text}
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                _POST_MESSAGE_URL,
                headers={"Authorization": f"Bearer {bot_token}"},
                json=payload,
            )
    except Exception:
        logger.warning("slack chat.postMessage request failed", exc_info=True)
        return None

    if not response.is_success:
        logger.warning(
            "slack chat.postMessage returned status %d: %s",
            response.status_code,
            response.text,
        )
        return None

    body = response.json()
    if body.get("ok") is not True:
        logger.warning("slack chat.postMessage returned ok=false: %s", body.get("error"))
        return None

    return body.get("ts")


async def send_http_webhook(
    url: str,
    payload: dict,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """POST a webhook only after pinning its host to a public DNS address."""
    try:
        target = httpx.URL(url)
        if (
            target.scheme not in {"http", "https"}
            or not target.host
            or target.username
            or target.password
        ):
            raise ValueError("invalid webhook URL")
        port = target.port or (443 if target.scheme == "https" else 80)
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(target.host, port, type=socket.SOCK_STREAM),
            timeout=_HTTP_TIMEOUT,
        )
        resolved_addresses = {
            ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            for _family, _type, _proto, _canonname, sockaddr in addresses
        }
        if not resolved_addresses or not all(_is_global(ip) for ip in resolved_addresses):
            raise ValueError("webhook destination did not resolve exclusively to public addresses")
        public_addresses = sorted(str(ip) for ip in resolved_addresses)
    except (TimeoutError, OSError, ValueError, httpx.InvalidURL):
        logger.warning("HTTP webhook destination rejected", exc_info=True)
        return False

    pinned_url = target.copy_with(host=public_addresses[0])
    default_port = 443 if target.scheme == "https" else 80
    host_name = f"[{target.host}]" if ":" in target.host else target.host
    host_header = host_name if port == default_port else f"{host_name}:{port}"
    owned_client = client is None
    if owned_client:
        client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT, trust_env=False)
    try:
        response = await client.post(
            pinned_url,
            headers={"Host": host_header},
            json=payload,
            follow_redirects=False,
            timeout=_HTTP_TIMEOUT,
            extensions={"sni_hostname": target.host},
        )
    except httpx.HTTPError:
        logger.warning("HTTP webhook request failed", exc_info=True)
        return False
    finally:
        if owned_client:
            await client.aclose()

    if not response.is_success:
        logger.warning("HTTP webhook returned status %d", response.status_code)
        return False
    return True
