"""Unit + integration tests for the invariant ratchet (hyqs/pipeline/invariants.py).

Pure filesystem + subprocess — no Postgres, matching the sast.py test style.
"""

from __future__ import annotations

import asyncio
import stat
import subprocess

from hyqs.pipeline import invariants


def _write_executable(path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _checks_dir(tmp_path):
    d = tmp_path / ".hyqs" / "invariants"
    d.mkdir(parents=True)
    return d


# --- discovery ---------------------------------------------------------


def test_discover_returns_empty_list_when_no_invariants_dir(tmp_path):
    assert invariants.discover(tmp_path) == []


def test_discover_sorts_by_filename_and_ignores_other_extensions(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(d / "b_check.sh", "#!/bin/sh\nexit 0\n")
    _write_executable(d / "a_check.py", "exit(0)\n")
    (d / "README.md").write_text("docs")
    (d / "notes.txt").write_text("notes")

    found = invariants.discover(tmp_path)

    assert [p.name for p in found] == ["a_check.py", "b_check.sh"]


# --- exit-code semantics -------------------------------------------------


def test_run_invariants_exit_0_is_pass(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(d / "check.sh", "#!/bin/sh\nexit 0\n")

    results = asyncio.run(invariants.run_invariants(tmp_path, "main", ["foo.py"]))

    assert len(results) == 1
    assert results[0].status == "pass"


def test_run_invariants_exit_1_is_fail_with_captured_output(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(d / "check.sh", "#!/bin/sh\necho 'banned pattern found'\nexit 1\n")

    results = asyncio.run(invariants.run_invariants(tmp_path, "main", ["foo.py"]))

    assert results[0].status == "fail"
    assert "banned pattern found" in results[0].output


def test_run_invariants_exit_78_is_skip(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(d / "check.sh", "#!/bin/sh\nexit 78\n")

    results = asyncio.run(invariants.run_invariants(tmp_path, "main", ["foo.py"]))

    assert results[0].status == "skip"


def test_run_invariants_other_exit_code_is_skip(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(d / "check.sh", "#!/bin/sh\nexit 3\n")

    results = asyncio.run(invariants.run_invariants(tmp_path, "main", ["foo.py"]))

    assert results[0].status == "skip"


# --- timeout -------------------------------------------------------------


def test_run_invariants_timeout_is_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(invariants, "CHECK_TIMEOUT", 0.2)
    d = _checks_dir(tmp_path)
    _write_executable(d / "check.sh", "#!/bin/sh\nsleep 5\nexit 0\n")

    results = asyncio.run(invariants.run_invariants(tmp_path, "main", ["foo.py"]))

    assert results[0].status == "skip"
    assert "timed out" in results[0].output


# --- stdin / env contract -------------------------------------------------


def test_run_invariants_passes_changed_files_via_stdin_and_env(tmp_path):
    d = _checks_dir(tmp_path)
    _write_executable(
        d / "check.sh",
        "#!/bin/sh\n"
        "echo \"STDIN:$(cat)\"\n"
        "echo \"ENV_FILES:$HYQS_CHANGED_FILES\"\n"
        "echo \"ENV_BASE:$HYQS_BASE_REF\"\n"
        "exit 1\n",
    )

    results = asyncio.run(
        invariants.run_invariants(tmp_path, "main", ["a.py", "b/c.py"])
    )

    output = results[0].output
    assert "STDIN:a.py\nb/c.py" in output
    assert "ENV_FILES:a.py\nb/c.py" in output
    assert "ENV_BASE:main" in output


# --- harness errors --------------------------------------------------------


def test_run_invariants_unreadable_script_is_skip_not_raise(tmp_path):
    d = _checks_dir(tmp_path)
    broken = d / "check.sh"
    broken.write_text("#!/bin/sh\nexit 0\n")
    # No read permission -> the interpreter can't open it (harness error, not a crash).
    broken.chmod(0o000)

    try:
        results = asyncio.run(invariants.run_invariants(tmp_path, "main", []))
    finally:
        broken.chmod(0o644)  # restore so tmp_path cleanup can remove it

    assert results[0].status == "skip"


# --- ensure_readme ----------------------------------------------------------


def test_ensure_readme_creates_file_when_absent(tmp_path):
    invariants.ensure_readme(tmp_path)

    readme = tmp_path / ".hyqs" / "invariants" / "README.md"
    assert readme.exists()
    assert "Contract" in readme.read_text()


def test_ensure_readme_does_not_overwrite_existing_file(tmp_path):
    d = _checks_dir(tmp_path)
    (d / "README.md").write_text("custom content")

    invariants.ensure_readme(tmp_path)

    assert (d / "README.md").read_text() == "custom content"


# --- integration: toy repo, fail -> fix -> pass -----------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_invariant_fails_on_bad_diff_and_passes_once_fixed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)

    d = _checks_dir(repo)
    _write_executable(
        d / "no_todo.sh",
        "#!/bin/sh\n"
        'if git diff --unified=0 "$HYQS_BASE_REF...HEAD" -- $(cat) | grep -q "^+.*TODO"; then\n'
        "  echo 'banned TODO marker introduced'\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    (repo / "app.py").write_text("print('hello')\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("branch", "-q", "-m", "main", cwd=repo)
    _git("checkout", "-q", "-b", "feature", cwd=repo)

    # Bad diff: introduces a banned TODO.
    (repo / "app.py").write_text("print('hello')\n# TODO: fix later\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "bad change", cwd=repo)

    bad_results = asyncio.run(invariants.run_invariants(repo, "main", ["app.py"]))
    assert any(r.status == "fail" for r in bad_results)

    # Fix: remove the TODO.
    (repo / "app.py").write_text("print('hello')\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "fix", cwd=repo)

    good_results = asyncio.run(invariants.run_invariants(repo, "main", ["app.py"]))
    assert all(r.status == "pass" for r in good_results)
