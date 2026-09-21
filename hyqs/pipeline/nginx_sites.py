"""Manage per-project nginx vhosts under /etc/nginx/hyqs.d/."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("hyqs.nginx_sites")

SITES_DIR: Path = Path("/etc/nginx/hyqs.d")
_VALIDATE_CMD: list[str] = ["sudo", "/usr/sbin/nginx", "-t"]
_RELOAD_CMD: list[str] = ["sudo", "systemctl", "reload", "nginx"]

_PROXY_PASS_RE = re.compile(r"proxy_pass http://127\.0\.0\.1:(\d+);")
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)

_DOMAINS_ENV = "HYQS_NGINX_DOMAINS"


def domain_ssl_snippets() -> dict[str, str]:
    """Ordered ``{domain: ssl-snippet}`` that per-project vhosts may be served on.

    Read from ``HYQS_NGINX_DOMAINS``: a comma-separated list where each entry is
    either ``domain`` or ``domain=snippet.conf``. Without an explicit snippet the
    filename is derived from the domain's first label (``example.com`` ->
    ``example-ssl.conf``), matching the naming convention deploy/ uses for the
    certbot-managed snippets.

    This doubles as the ALLOWLIST enforced by ``_validate_domains``: a domain not
    configured here can never reach a rendered nginx config. Unset therefore means
    no domain is permitted and vhost registration fails closed with an actionable
    message, rather than falling back to a guessed hostname.
    """
    snippets: dict[str, str] = {}
    for entry in os.environ.get(_DOMAINS_ENV, "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        domain, _, snippet = entry.partition("=")
        domain = domain.strip().lower()
        snippet = snippet.strip()
        if _HOSTNAME_RE.fullmatch(domain) is None:
            raise ValueError(f"invalid domain in {_DOMAINS_ENV}: {domain!r}")
        snippets[domain] = snippet or f"{domain.split('.')[0]}-ssl.conf"
    return snippets


def base_domain() -> str:
    """The first configured domain — the one every project subdomain gets."""
    snippets = domain_ssl_snippets()
    if not snippets:
        raise ValueError(
            f"{_DOMAINS_ENV} is not set, so no nginx vhost can be rendered. "
            f"Set it to the domain projects are served under, e.g. "
            f"{_DOMAINS_ENV}=example.com"
        )
    return next(iter(snippets))


def normalize_port(port: object) -> int:
    """Return a valid TCP port, rejecting values unsafe for nginx rendering."""
    try:
        normalized = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid nginx port: {port!r}") from exc
    if not 1 <= normalized <= 65535:
        raise ValueError(f"nginx port must be between 1 and 65535: {normalized}")
    return normalized


def _validate_domains(domains: object) -> list[str]:
    if not isinstance(domains, list):
        raise ValueError("nginx domains must be a list of hostnames")
    for domain in domains:
        if not isinstance(domain, str):
            raise ValueError("each nginx domain must be a string")
        if _HOSTNAME_RE.fullmatch(domain) is None:
            raise ValueError(f"invalid nginx domain: {domain!r}")
    configured = domain_ssl_snippets()
    for domain in domains:
        if domain not in configured:
            raise ValueError(f"no SSL snippet configured for nginx domain: {domain}")
    return domains


def resolve_domains(deploy_cfg: dict) -> list[str]:
    """The domain list ``register_site``/``render_site`` expect for a project.

    The configured base domain is always first; any ``extra_domains`` declared
    in the project's ``deploy_config`` are appended in the order given.
    """
    extra_domains = deploy_cfg.get("extra_domains", [])
    _validate_domains(extra_domains)
    return _validate_domains([base_domain(), *extra_domains])


def render_site(slug: str, port: object, domains: list[str] | None = None) -> str:
    port = normalize_port(port)
    if domains is None:
        domains = [base_domain()]
    domains = _validate_domains(domains)
    blocks = []
    snippets = domain_ssl_snippets()
    for domain in domains:
        snippet = snippets[domain]
        blocks.append(
            f"""\
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name {slug}.{domain};
    include snippets/{snippet};

    location / {{
        proxy_pass http://127.0.0.1:{port};
        include snippets/proxy-common.conf;
    }}
}}
server {{
    listen 80;
    listen [::]:80;
    server_name {slug}.{domain};
    return 301 https://$host$request_uri;
}}
"""
        )
    return "".join(blocks)


async def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def register_site(
    slug: str,
    port: object,
    *,
    domains: list[str] | None = None,
    sites_dir: Path = SITES_DIR,
    validate_cmd: list[str] = _VALIDATE_CMD,
    reload_cmd: list[str] = _RELOAD_CMD,
) -> None:
    port = normalize_port(port)
    if domains is None:
        domains = [base_domain()]
    domains = _validate_domains(domains)
    conf_path = sites_dir / f"{slug}.conf"
    conf_path.write_text(render_site(slug, port, domains))
    code, out = await _run_cmd(validate_cmd)
    if code != 0:
        conf_path.unlink(missing_ok=True)
        raise RuntimeError(f"nginx -t failed for {slug}: {out}")
    await _run_cmd(reload_cmd)
    hosts = ", ".join(f"{slug}.{domain}" for domain in domains)
    log.info("registered nginx site %s -> :%d", hosts, port)


def parse_vhost_port(slug: str, *, sites_dir: Path = SITES_DIR) -> int | None:
    """The upstream port a project's nginx vhost proxies to, or None.

    None covers: no vhost file for this slug, the file is unreadable, or its
    contents don't match the `proxy_pass http://127.0.0.1:<port>;` pattern
    ``render_site`` emits.
    """
    conf_path = sites_dir / f"{slug}.conf"
    try:
        content = conf_path.read_text()
    except FileNotFoundError:
        return None
    match = _PROXY_PASS_RE.search(content)
    return int(match.group(1)) if match else None


def compute_port_drift(
    configured_port: int | None,
    published_port: int | None,
    vhost_port: int | None,
    *,
    repo_path: str = "",
) -> dict:
    """Pure 3-way comparison of a project's port config vs. reality.

    ``published_port`` (the container's actual host port) vs. ``vhost_port``
    (what nginx is configured to forward to) is the classic dead-port
    misroute and takes priority; ``configured_port`` vs. ``vhost_port`` catches
    stale deploy_config. Either comparison is skipped if either side is
    missing — can't assert drift without both sides.
    """
    if published_port is not None and vhost_port is not None and published_port != vhost_port:
        return {
            "configured_port": configured_port,
            "published_port": published_port,
            "vhost_port": vhost_port,
            "mismatch": True,
            "detail": (
                f"container is on :{published_port} but nginx expects :{vhost_port}; "
                f"run `PORT={vhost_port} docker compose up -d proxy` in {repo_path}"
            ),
        }
    if configured_port is not None and vhost_port is not None and configured_port != vhost_port:
        return {
            "configured_port": configured_port,
            "published_port": published_port,
            "vhost_port": vhost_port,
            "mismatch": True,
            "detail": (
                f"configured port :{configured_port} does not match nginx vhost port "
                f":{vhost_port} for {repo_path}"
            ),
        }
    return {
        "configured_port": configured_port,
        "published_port": published_port,
        "vhost_port": vhost_port,
        "mismatch": False,
        "detail": "",
    }


async def remove_site(
    slug: str,
    *,
    sites_dir: Path = SITES_DIR,
    validate_cmd: list[str] = _VALIDATE_CMD,
    reload_cmd: list[str] = _RELOAD_CMD,
) -> None:
    conf_path = sites_dir / f"{slug}.conf"
    conf_path.unlink(missing_ok=True)
    code, out = await _run_cmd(validate_cmd)
    if code != 0:
        raise RuntimeError(f"nginx -t failed after removing {slug}: {out}")
    await _run_cmd(reload_cmd)
    log.info("removed nginx site for %s", slug)
