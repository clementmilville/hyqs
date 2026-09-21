from __future__ import annotations

from hyqs.pipeline.secret_provider import FileSecretProvider


def test_directory_provider_reads_value_from_file(tmp_path):
    (tmp_path / "DB_PASSWORD").write_text("hunter2\n")
    provider = FileSecretProvider(tmp_path)

    assert provider.get("DB_PASSWORD") == "hunter2"


def test_directory_provider_returns_none_for_missing_file(tmp_path):
    provider = FileSecretProvider(tmp_path)

    assert provider.get("NOT_THERE") is None


def test_file_provider_parses_env_style_lines(tmp_path):
    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "DB_PASSWORD=hunter2",
                "API_KEY='abc123'",
                'QUOTED="with spaces"',
            ]
        )
    )
    provider = FileSecretProvider(env_file)

    assert provider.get("DB_PASSWORD") == "hunter2"
    assert provider.get("API_KEY") == "abc123"
    assert provider.get("QUOTED") == "with spaces"
    assert provider.get("MISSING") is None


def test_provider_for_nonexistent_path_returns_none(tmp_path):
    provider = FileSecretProvider(tmp_path / "does-not-exist")

    assert provider.get("ANYTHING") is None
