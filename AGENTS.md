# Hyqs agent guidance

Read and follow `CONVENTIONS.md` before changing code. It is this repository's
constitution and contains the detailed Python, database, pipeline, frontend,
design-system, testing, and review rules. When instructions conflict, preserve
the stricter safety or compatibility requirement.

## Project map

- `hyqs/core/`: persistent conversational-agent sessions and scheduling.
- `hyqs/pipeline/`: provider-agnostic autonomous build pipeline, job store,
  stages, contracts, deployment, and Claude/Codex backends.
- `hyqs/web/`: Starlette API, MCP server, authentication, and web application.
- `hyqs/web/frontend/`: React/Vite frontend.
- `hyqs/tools/`: in-process MCP tools.
- `hyqs/skills/`: agent skills shipped with Hyqs.
- `tests/`: Python tests. Database tests use a real PostgreSQL database through
  the shared fixtures in `tests/conftest.py`.
- `deploy/`: Docker and systemd deployment assets.

## Working rules

- Inspect the nearest existing implementation and its tests before editing.
- Keep changes scoped to the requested behavior; do not rewrite unrelated code.
- Preserve provider neutrality in pipeline stages. Provider-specific behavior
  belongs in `hyqs/pipeline/providers.py` or its provider module, such as
  `hyqs/pipeline/codex.py`.
- Keep SQL and persistence operations inside `hyqs/pipeline/store.py`. Return
  typed models rather than leaking database rows.
- Database migrations must be additive and compatible with old and new workers
  running concurrently. Do not drop, rename, or incompatibly alter live columns
  in a single release.
- Do not commit or merge from inside an agent stage; the deterministic runner
  owns git plumbing and merge gates.
- Never weaken, remove, or bypass tests, invariants, security gates, contract
  validation, or rules in `CONVENTIONS.md` merely to make a change pass.
- Do not edit `.env`, credentials, runtime data, or local Claude/Codex settings.
- Do not modify generated frontend output under `hyqs/web/frontend/dist/`.
- When making repository changes locally, commit and push them to the remote
  before considering the work complete. Leave the deploy checkout on a clean
  `main` synchronized with `origin/main`; use a pushed branch when changes are
  not ready to land on `main`.

## Validation

Use the smallest relevant checks while iterating, then broaden them in
proportion to the change.

```bash
# Python tests (a specific test is preferred while iterating)
uv run pytest tests/test_<area>.py

# Python lint and formatting checks
uv run ruff check .
uv run ruff format --check .

# Frontend tests and production build
cd hyqs/web/frontend
npm test
npm run build
```

The full Python suite may require `HYQS_DB_URL` or `DATABASE_URL` pointing to a
test-safe PostgreSQL instance. Do not point tests at production or shared
non-test data. If a required service is unavailable, run all unaffected checks
and report exactly what was not verified.

## Definition of done

- The requested behavior is implemented without unrelated changes.
- Relevant regression tests are added or updated.
- Applicable tests, lint, formatting, and frontend build checks pass.
- Public behavior, environment variables, or operational steps are documented
  when they change.
- The final handoff states what changed, what was verified, and any remaining
  risk or unrun validation.
