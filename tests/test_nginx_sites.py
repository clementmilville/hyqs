"""Tests for nginx_sites.parse_vhost_port and compute_port_drift (Epic 49
job 2) — the port/vhost drift detector that would have caught the Acme
502 (container published on :8080 while the vhost forwarded to :8364).
"""

from __future__ import annotations

import asyncio

import pytest

from hyqs.pipeline.nginx_sites import (
    base_domain,
    compute_port_drift,
    domain_ssl_snippets,
    normalize_port,
    parse_vhost_port,
    register_site,
    render_site,
    resolve_domains,
)


@pytest.fixture(autouse=True)
def _nginx_domains(monkeypatch):
    """Pin the configured domain allowlist for every test in this module.

    The domains are read from HYQS_NGINX_DOMAINS at call time, so without this
    the suite would depend on the host's .env.
    """
    # Two forms at once: example.com derives its snippet from the first label,
    # example.org names one explicitly. Distinct snippets so the multi-domain
    # render test actually proves per-domain snippet selection.
    monkeypatch.setenv(
        "HYQS_NGINX_DOMAINS", "example.com,example.org=example-org-ssl.conf"
    )


def test_parse_vhost_port_reads_port_from_rendered_conf(tmp_path):
    (tmp_path / "acme.conf").write_text(render_site("acme", 8364))

    assert parse_vhost_port("acme", sites_dir=tmp_path) == 8364


def test_parse_vhost_port_returns_none_for_missing_file(tmp_path):
    assert parse_vhost_port("nope", sites_dir=tmp_path) is None


def test_parse_vhost_port_returns_none_when_pattern_absent(tmp_path):
    (tmp_path / "broken.conf").write_text("server { listen 443; }\n")

    assert parse_vhost_port("broken", sites_dir=tmp_path) is None


def test_compute_port_drift_all_match_is_no_mismatch():
    result = compute_port_drift(8364, 8364, 8364, repo_path="/home/apps/acme")

    assert result["mismatch"] is False
    assert result["detail"] == ""


def test_compute_port_drift_published_vs_vhost_mismatch():
    result = compute_port_drift(8364, 8080, 8364, repo_path="/home/apps/acme")

    assert result["mismatch"] is True
    assert result["configured_port"] == 8364
    assert result["published_port"] == 8080
    assert result["vhost_port"] == 8364
    assert (
        result["detail"] == "container is on :8080 but nginx expects :8364; "
        "run `PORT=8364 docker compose up -d proxy` in /home/apps/acme"
    )


def test_compute_port_drift_configured_vs_vhost_mismatch_when_published_missing():
    result = compute_port_drift(8000, None, 8364, repo_path="/home/apps/acme")

    assert result["mismatch"] is True
    assert "8000" in result["detail"]
    assert "8364" in result["detail"]


def test_compute_port_drift_missing_side_is_not_a_mismatch():
    assert compute_port_drift(None, None, None)["mismatch"] is False
    assert compute_port_drift(8364, None, None)["mismatch"] is False
    assert compute_port_drift(None, 8080, None)["mismatch"] is False


def test_render_site_default_domain_matches_single_domain_format():
    assert render_site("acme", 8364) == render_site(
        "acme", 8364, domains=["example.com"]
    )
    rendered = render_site("acme", 8364)
    assert (
        rendered
        == """\
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name acme.example.com;
    include snippets/example-ssl.conf;

    location / {
        proxy_pass http://127.0.0.1:8364;
        include snippets/proxy-common.conf;
    }
}
server {
    listen 80;
    listen [::]:80;
    server_name acme.example.com;
    return 301 https://$host$request_uri;
}
"""
    )


def test_render_site_multi_domain_emits_one_block_pair_per_domain():
    rendered = render_site("acme", 9001, domains=["example.com", "example.org"])

    assert (
        rendered
        == """\
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name acme.example.com;
    include snippets/example-ssl.conf;

    location / {
        proxy_pass http://127.0.0.1:9001;
        include snippets/proxy-common.conf;
    }
}
server {
    listen 80;
    listen [::]:80;
    server_name acme.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name acme.example.org;
    include snippets/example-org-ssl.conf;

    location / {
        proxy_pass http://127.0.0.1:9001;
        include snippets/proxy-common.conf;
    }
}
server {
    listen 80;
    listen [::]:80;
    server_name acme.example.org;
    return 301 https://$host$request_uri;
}
"""
    )


@pytest.mark.parametrize("port", ["not-a-port", "8080; include /etc/shadow;"])
def test_render_site_rejects_non_integer_port(port):
    with pytest.raises(ValueError, match="invalid nginx port"):
        render_site("acme", port)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_render_site_rejects_out_of_range_port(port):
    with pytest.raises(ValueError, match="between 1 and 65535"):
        render_site("acme", port)


def test_render_site_coerces_integer_string_port():
    rendered = render_site("acme", "9001")

    assert "proxy_pass http://127.0.0.1:9001;" in rendered
    assert normalize_port("9001") == 9001


@pytest.mark.parametrize(
    "domains",
    [
        "example.com",
        ["example.com", 7],
        ["example.com; include /etc/shadow;"],
        ["unknown.example"],
    ],
)
def test_render_site_rejects_invalid_domains(domains):
    with pytest.raises(ValueError):
        render_site("acme", 9001, domains=domains)


def test_resolve_domains_defaults_to_base_domain_only():
    assert resolve_domains({}) == ["example.com"]


def test_resolve_domains_appends_extra_domains_in_order():
    assert resolve_domains({"port": 1, "extra_domains": ["example.org"]}) == [
        "example.com",
        "example.org",
    ]


def test_resolve_domains_ignores_missing_extra_domains_key():
    assert resolve_domains({"port": 8100}) == ["example.com"]


@pytest.mark.parametrize(
    "extra_domains",
    [
        None,
        "example.org",
        [7],
        ["example.org; include /etc/shadow;"],
        ["unsupported.example"],
    ],
)
def test_resolve_domains_rejects_invalid_extra_domains(extra_domains):
    with pytest.raises(ValueError):
        resolve_domains({"extra_domains": extra_domains})


def test_register_site_writes_conf_with_a_server_block_per_configured_domain(tmp_path, monkeypatch):
    async def _fake_run_cmd(cmd):
        return 0, ""

    monkeypatch.setattr("hyqs.pipeline.nginx_sites._run_cmd", _fake_run_cmd)

    asyncio.run(
        register_site(
            "acme",
            9001,
            domains=resolve_domains({"extra_domains": ["example.org"]}),
            sites_dir=tmp_path,
        )
    )

    content = (tmp_path / "acme.conf").read_text()
    assert "server_name acme.example.com;" in content
    assert "server_name acme.example.org;" in content


def test_register_site_rolls_back_conf_on_validation_failure(tmp_path, monkeypatch):
    async def _fake_run_cmd(cmd):
        return 1, "syntax error"

    monkeypatch.setattr("hyqs.pipeline.nginx_sites._run_cmd", _fake_run_cmd)

    with pytest.raises(RuntimeError):
        asyncio.run(
            register_site(
                "acme",
                9001,
                domains=["example.com"],
                sites_dir=tmp_path,
            )
        )

    assert not (tmp_path / "acme.conf").exists()


@pytest.mark.parametrize("port", ["invalid", 0, 65536, "8080; include /etc/shadow;"])
def test_register_site_rejects_invalid_port_before_side_effects(tmp_path, monkeypatch, port):
    called = False

    async def _fake_run_cmd(cmd):
        nonlocal called
        called = True
        return 0, ""

    monkeypatch.setattr("hyqs.pipeline.nginx_sites._run_cmd", _fake_run_cmd)

    with pytest.raises(ValueError):
        asyncio.run(register_site("acme", port, sites_dir=tmp_path))

    assert not (tmp_path / "acme.conf").exists()
    assert called is False


# --- configured-domain allowlist (personal-domain parameterization) ---------


def test_domain_ssl_snippets_derives_snippet_from_first_label():
    assert domain_ssl_snippets()["example.com"] == "example-ssl.conf"


def test_domain_ssl_snippets_honours_an_explicit_snippet():
    assert domain_ssl_snippets()["example.org"] == "example-org-ssl.conf"


def test_base_domain_is_the_first_configured_entry():
    assert base_domain() == "example.com"


def test_unset_domains_fails_closed_rather_than_guessing(monkeypatch):
    """No configured domain must mean no vhost, not a fallback hostname."""
    monkeypatch.delenv("HYQS_NGINX_DOMAINS", raising=False)

    assert domain_ssl_snippets() == {}
    with pytest.raises(ValueError, match="HYQS_NGINX_DOMAINS"):
        base_domain()
    with pytest.raises(ValueError, match="HYQS_NGINX_DOMAINS"):
        resolve_domains({})


def test_domain_not_in_the_allowlist_is_rejected(monkeypatch):
    monkeypatch.setenv("HYQS_NGINX_DOMAINS", "example.com")

    with pytest.raises(ValueError, match="no SSL snippet configured"):
        render_site("acme", 9001, domains=["evil.example.org"])


def test_malformed_entry_in_the_env_is_rejected(monkeypatch):
    monkeypatch.setenv("HYQS_NGINX_DOMAINS", "not a hostname")

    with pytest.raises(ValueError, match="invalid domain"):
        domain_ssl_snippets()
