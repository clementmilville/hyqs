"""FastAPI scaffold blueprint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import invariants
from .bare import _BASE_GITIGNORE, COMPOSE_YML, ENV_EXAMPLE

_FASTAPI_GITIGNORE = """\

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

_MAIN_PY = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}
"""

_TEST_HEALTH_PY = """\
import pytest
from httpx import AsyncClient, ASGITransport

from main import app


@pytest.mark.asyncio
async def test_health_returns_ok():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
"""

_DOCKERFILE = """\
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn

COPY . .

EXPOSE 8080
ENV DATABASE_URL=sqlite+aiosqlite:////data/db.sqlite
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
"""


def _pyproject_toml(name: str, description: str) -> str:
    return f'''\
[project]
name = "{name}"
version = "0.1.0"
description = "{description}"
dependencies = [
    "fastapi",
    "uvicorn",
    "httpx",
    "pytest",
    "pytest-asyncio",
    "anyio",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
'''


@dataclass
class FastAPIBlueprint:
    name: str = "fastapi"
    description: str = "Minimal FastAPI service"
    default_port: int = 8080

    def scaffold(self, path: Path, name: str, description: str, spec: dict | None = None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "main.py").write_text(_MAIN_PY)
        tests_dir = path / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_health.py").write_text(_TEST_HEALTH_PY)
        (path / "Dockerfile").write_text(_DOCKERFILE)
        (path / "pyproject.toml").write_text(_pyproject_toml(name, description))
        (path / ".gitignore").write_text(_BASE_GITIGNORE + _FASTAPI_GITIGNORE)
        (path / "docker-compose.yml").write_text(COMPOSE_YML)
        (path / ".env.example").write_text(ENV_EXAMPLE)
        invariants.ensure_readme(path)
