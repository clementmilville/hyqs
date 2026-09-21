"""Tests for the idempotent generated-block helper and the CSS-token invariant template."""

from __future__ import annotations

import subprocess
import sys

from hyqs.pipeline.conventions_block import (
    BEGIN_MARKER,
    CSS_TOKEN_INVARIANT_TEMPLATE,
    END_MARKER,
    replace_generated_block,
)


def test_replace_generated_block_appends_when_markers_absent():
    content = "# CONVENTIONS\n\nSome hand-written rule.\n"
    result = replace_generated_block(content, "generated stuff")

    assert result.startswith(content)
    assert BEGIN_MARKER in result
    assert END_MARKER in result
    assert "generated stuff" in result


def test_replace_generated_block_replaces_existing_block_preserving_surrounding_text():
    content = (
        "# CONVENTIONS\n\nBefore text.\n\n"
        f"{BEGIN_MARKER}\nold generated\n{END_MARKER}\n\n"
        "After text.\n"
    )

    result = replace_generated_block(content, "new generated")

    assert "Before text." in result
    assert "After text." in result
    assert "old generated" not in result
    assert "new generated" in result


def test_replace_generated_block_is_idempotent():
    content = "# CONVENTIONS\n\nSome hand-written rule.\n"

    first = replace_generated_block(content, "generated stuff")
    second = replace_generated_block(first, "generated stuff")

    assert first == second


def _run_check(script_path, cwd, changed_files):
    stdin_text = "\n".join(changed_files)
    return subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(cwd),
        input=stdin_text,
        capture_output=True,
        text=True,
        env={"HYQS_CHANGED_FILES": stdin_text, "PATH": "/usr/bin:/bin"},
        timeout=30,
    )


def test_css_token_template_skips_when_stylesheet_not_changed(tmp_path):
    script = tmp_path / "check_css_tokens.py"
    script.write_text(CSS_TOKEN_INVARIANT_TEMPLATE)
    (tmp_path / "styles.css").write_text(":root { --brand-color: red; }\n.foo { color: red; }\n")

    result = _run_check(script, tmp_path, ["some/other/file.py"])

    assert result.returncode == 78


def test_css_token_template_fails_when_conventions_stale(tmp_path):
    script = tmp_path / "check_css_tokens.py"
    script.write_text(CSS_TOKEN_INVARIANT_TEMPLATE)
    (tmp_path / "styles.css").write_text(":root { --brand-color: red; }\n.foo { color: red; }\n")
    (tmp_path / "CONVENTIONS.md").write_text("# CONVENTIONS\n\nSome rule.\n")

    result = _run_check(script, tmp_path, ["styles.css"])

    assert result.returncode == 1
    assert "--brand-color" in result.stdout
    assert ".foo" in result.stdout


def test_css_token_template_passes_once_block_matches(tmp_path):
    script = tmp_path / "check_css_tokens.py"
    script.write_text(CSS_TOKEN_INVARIANT_TEMPLATE)
    (tmp_path / "styles.css").write_text(":root { --brand-color: red; }\n.foo { color: red; }\n")
    (tmp_path / "CONVENTIONS.md").write_text("# CONVENTIONS\n\nSome rule.\n")

    first = _run_check(script, tmp_path, ["styles.css"])
    assert first.returncode == 1
    regenerated = first.stdout.split("Regenerated block:\n\n", 1)[1]
    (tmp_path / "CONVENTIONS.md").write_text(regenerated)

    second = _run_check(script, tmp_path, ["styles.css"])

    assert second.returncode == 0
