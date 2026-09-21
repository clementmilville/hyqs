"""Tests for Config.from_env's HYQS_GEOIP_DB handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyqs.config import Config


def test_from_env_geoip_db_defaults_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HYQS_GEOIP_DB", raising=False)

    config = Config.from_env()

    assert config.geoip_db == tmp_path / "geoip" / "dbip-city-lite.mmdb"


def test_from_env_geoip_db_uses_configured_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HYQS_GEOIP_DB", "~/custom/geoip.mmdb")

    config = Config.from_env()

    assert config.geoip_db == Path("~/custom/geoip.mmdb").expanduser()


def test_from_env_trusted_proxy_depth_defaults_to_one(monkeypatch):
    monkeypatch.delenv("HYQS_TRUSTED_PROXY_DEPTH", raising=False)

    config = Config.from_env()

    assert config.trusted_proxy_depth == 1


def test_from_env_trusted_proxy_depth_uses_configured_value(monkeypatch):
    monkeypatch.setenv("HYQS_TRUSTED_PROXY_DEPTH", "3")

    config = Config.from_env()

    assert config.trusted_proxy_depth == 3


def test_from_env_max_request_body_bytes_defaults_to_25mb(monkeypatch):
    monkeypatch.delenv("HYQS_MAX_REQUEST_BODY_BYTES", raising=False)

    config = Config.from_env()

    assert config.max_request_body_bytes == 25 * 1024 * 1024


def test_from_env_max_request_body_bytes_uses_configured_value(monkeypatch):
    monkeypatch.setenv("HYQS_MAX_REQUEST_BODY_BYTES", "1048576")

    config = Config.from_env()

    assert config.max_request_body_bytes == 1048576


# --- .env.example must actually be loadable ---------------------------------
#
# A fresh install copies .env.example verbatim, so every documented placeholder
# lands in the environment as "". `int(os.environ.get(NAME, "8"))` never sees
# its own default in that case and dies with
# `invalid literal for int() with base 10: ''`, naming no variable, before
# logging is configured — crash-looping hyqs-web on a brand-new VPS.


def _load_env_example(monkeypatch, tmp_path):
    """Put every assignment in .env.example into the environment, verbatim."""
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    for line in env_example.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        monkeypatch.setenv(key.strip(), value.strip())
    # Keep the filesystem side effects inside the test.
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HYQS_PROJECTS_DIR", str(tmp_path / "projects"))


def test_config_loads_from_a_verbatim_env_example(monkeypatch, tmp_path):
    """The exact thing deploy/install.sh does on a fresh host."""
    _load_env_example(monkeypatch, tmp_path)

    config = Config.from_env()

    # Empty placeholders must fall back to the documented defaults.
    assert config.pipeline_incident_analyst_scan_budget == 8
    assert config.pipeline_incident_analyst_job_cap == 6


def test_empty_numeric_placeholder_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HYQS_PIPELINE_CONCURRENCY", "")
    monkeypatch.setenv("HYQS_PIPELINE_JOB_STALE_HOURS", "   ")

    config = Config.from_env()

    assert config.pipeline_concurrency == 1
    assert config.pipeline_job_stale_hours == 24.0


def test_a_configured_numeric_value_is_still_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HYQS_PIPELINE_CONCURRENCY", "4")

    config = Config.from_env()

    assert config.pipeline_concurrency == 4


def test_a_garbage_numeric_value_names_the_variable(monkeypatch, tmp_path):
    """A real misconfiguration must still fail — but say which setting."""
    monkeypatch.setenv("HYQS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HYQS_PIPELINE_CONCURRENCY", "lots")

    with pytest.raises(ValueError, match="HYQS_PIPELINE_CONCURRENCY"):
        Config.from_env()


def test_from_env_github_org_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("HYQS_GITHUB_ORG", raising=False)

    config = Config.from_env()

    assert config.github_org == ""


def test_from_env_github_org_is_read_and_stripped(monkeypatch):
    monkeypatch.setenv("HYQS_GITHUB_ORG", "  my-org  ")

    config = Config.from_env()

    assert config.github_org == "my-org"
