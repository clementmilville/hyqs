import json
from pathlib import Path

from .bare import BareBlueprint

_NODE_GITIGNORE = """\

# Node
node_modules/
dist/
build/
npm-debug.log*
yarn-error.log
.cache/

# Generated databases
*.db
*.sqlite
*.sqlite3

# App data
data/
uploads/
"""

_DOCKERFILE = """\
FROM node:20-slim
WORKDIR /app
COPY package*.json ./
RUN npm install --production 2>/dev/null || true
COPY . .
EXPOSE 8080
CMD ["node", "src/index.js"]
"""


class NodeBlueprint:
    def scaffold(self, path: Path, name: str, description: str, spec: dict) -> None:
        BareBlueprint().scaffold(path, name, description, spec)
        with (path / ".gitignore").open("a") as f:
            f.write(_NODE_GITIGNORE)
        # Overwrite the bare placeholder Dockerfile with the Node-specific one.
        (path / "Dockerfile").write_text(_DOCKERFILE)
        pkg = {"name": name, "version": "0.1.0", "private": True}
        (path / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
        src = path / "src"
        src.mkdir(exist_ok=True)
        (src / "index.js").write_text("")
