# Minimal image for a standalone deploy host: `hyqs-pipeline --deployer`
# (see hyqs/pipeline/__main__.py) runs a constrained worker that only ever
# claims the DEPLOY stage, pinned to this host via HYQS_HOST_NAME. That stage
# is deterministic — it drives `docker build`/`docker run` against the HOST's
# docker.sock (mounted at container run time, see deploy/hyqs-deployer.service)
# and never invokes an AI provider. So this image intentionally contains:
#   - the docker CLI, to talk to the mounted host docker.sock (no daemon runs
#     INSIDE this container — see the apt-get comment below)
#   - git, for the pipeline's own gitops helpers (hyqs/pipeline/gitops.py)
#   - the installed hyqs package itself
# and nothing else: no project/user repo source is baked in (the deploy stage
# operates on the pipeline's own managed clone, fetched at runtime), and no
# ANTHROPIC_API_KEY / CODEX_* / other AI-provider credential is declared,
# required, or read by --deployer mode.
FROM python:3.12-slim

# `docker-cli` is the client only — no dockerd/containerd, matching this
# image's job: it just calls `docker` against the HOST's docker.sock,
# bind-mounted in at `docker run` time (see deploy/hyqs-deployer.service).
# git is needed for the pipeline's own worktree/clone plumbing, not for any
# project source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
# README.md is required alongside pyproject.toml/uv.lock: hatchling (this
# package's build backend) validates `readme = "README.md"` at install time
# even for an editable install. It is package metadata, not project source.
COPY pyproject.toml uv.lock README.md ./
COPY hyqs/ hyqs/

RUN uv sync --frozen --no-dev

# Invoke the synced venv's entry point directly rather than `uv run`: `uv run`
# re-syncs against pyproject.toml on every invocation, which would silently
# pull in the [dependency-groups] dev tools (ruff, semgrep, pytest, ...) at
# container start — needing network access and bloating a "thin" image.
ENTRYPOINT ["/app/.venv/bin/hyqs-pipeline", "--deployer"]
