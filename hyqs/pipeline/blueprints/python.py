import re
from pathlib import Path

from .bare import BareBlueprint

_PYTHON_GITIGNORE = """\

# Python
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.mypy_cache/
.ruff_cache/
*.egg-info/
build/
dist/

# Generated databases
*.db
*.sqlite
*.sqlite3

# App data
data/
uploads/
"""

_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
COPY . .
EXPOSE 8080
CMD ["python3", "-m", "http.server", "8080"]
"""


def _pkg_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "project"


class PythonBlueprint:
    def scaffold(self, path: Path, name: str, description: str, spec: dict) -> None:
        BareBlueprint().scaffold(path, name, description, spec)
        with (path / ".gitignore").open("a") as f:
            f.write(_PYTHON_GITIGNORE)
        # Overwrite the bare placeholder Dockerfile with the Python-specific one.
        (path / "Dockerfile").write_text(_DOCKERFILE)
        pkg = _pkg_name(name)
        pyproject = (
            "[build-system]\n"
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.backends.legacy:build"\n\n'
            "[project]\n"
            f'name = "{pkg}"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
        (path / "pyproject.toml").write_text(pyproject)
        src_pkg = path / "src" / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        (src_pkg / "__init__.py").write_text("")
        tests_dir = path / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("")
