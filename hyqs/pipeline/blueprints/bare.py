from pathlib import Path

from .. import invariants

_CONVENTIONS_BODY = (
    "# Coding Conventions\n\n"
    "## Naming\n\n<!-- Describe naming rules here -->\n\n"
    "## Error Handling\n\n<!-- Describe error handling approach -->\n\n"
    "## Testing\n\n<!-- Describe test layout and conventions -->\n\n"
    "## Code Style\n\n<!-- Linting, formatting, type-annotation rules -->\n\n"
    "## Dependencies\n\n<!-- How to add / vet new dependencies -->\n"
)

_BASE_GITIGNORE = """\
# OS / editor
.DS_Store
Thumbs.db
.idea/
.vscode/

# Virtualenvs
.venv/
venv/

# Secrets
.env
.env.*
.env.secrets

# App data (lives in Docker volume — never commit)
data/
uploads/
*.sqlite
*.sqlite3
"""

# Shared docker-compose template — used by all stacks and the backfill.
COMPOSE_YML = """\
services:
  app:
    build: .
    container_name: "${CONTAINER_NAME:-app}"
    ports:
      - "127.0.0.1:${PORT:-8080}:8080"
    restart: unless-stopped
    env_file: .env.secrets
    volumes:
      - app_data:/data
    user: "65534:65534"
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    mem_limit: 256m
    cpus: "0.5"

volumes:
  app_data:
"""

# Shared .env.example — used by all stacks and the backfill.
ENV_EXAMPLE = """\
# Required environment variables — copy to .env.secrets and fill in real values.
# Never commit .env.secrets.

PORT=8080
"""

_DOCKERFILE = """\
FROM python:3.12-slim
WORKDIR /app
COPY . .
EXPOSE 8080
CMD ["python3", "-m", "http.server", "8080"]
"""

_README_TEMPLATE = """\
# {name}

{description}

## Deploy

```sh
cp .env.example .env.secrets  # fill in real values
docker compose up -d --build
```
"""


class BareBlueprint:
    def scaffold(self, path: Path, name: str, description: str, spec: dict) -> None:
        desc = description or ""
        (path / "README.md").write_text(_README_TEMPLATE.format(name=name, description=desc))
        (path / "CONVENTIONS.md").write_text(_CONVENTIONS_BODY)
        (path / ".gitignore").write_text(_BASE_GITIGNORE)
        (path / "docker-compose.yml").write_text(COMPOSE_YML)
        (path / ".env.example").write_text(ENV_EXAMPLE)
        (path / "Dockerfile").write_text(_DOCKERFILE)
        invariants.ensure_readme(path)
