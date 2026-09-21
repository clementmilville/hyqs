# hyqs-ai Coding Conventions

Prescriptive rules for this repo. BUILD and FIX agents read this file; follow
it exactly. When in doubt, copy the nearest working example.

---

## 1. Naming

- Modules, functions, variables: `snake_case`
- Classes: `PascalCase`
- Constants / `_SYS` prompts: `UPPER_SNAKE_CASE`
- Private helpers (not exported): leading underscore (`_load_conventions`)
- Imports: always fully-qualified (`from hyqs.pipeline.store import JobStore`),
  never star imports
- One public concept per file; keep files under ~300 lines

```python
# Good
from hyqs.pipeline.store import JobStore
from hyqs.pipeline.models import Job, Stage

store = JobStore(dsn)

# Bad
from hyqs.pipeline.store import *
import hyqs.pipeline.store as s
```

---

## 2. Error Handling

- Let exceptions propagate to the caller; do not swallow them
- Never use a bare `except:` — always name the exception type
- Catch only what you can handle; re-raise anything unexpected
- Use `ValueError` for invalid inputs, `RuntimeError` for broken invariants

```python
# Good
try:
    text = path.read_text()
except FileNotFoundError:
    return ""

# Bad
try:
    text = path.read_text()
except:          # bare — hides bugs
    return ""

try:
    text = path.read_text()
except Exception:  # too broad — catches KeyboardInterrupt etc.
    return ""
```

---

## 3. Adding a Store Method

Store methods live in `hyqs/pipeline/store.py` inside `JobStore`. They must:

1. Accept only Python scalars / dataclasses — no raw SQL outside `store.py`
2. Execute SQL via `conn.execute(...)` inside a `with self._pool.connection() as conn:` block
3. Return a dataclass or `list[dataclass]` (never a raw `Row`/`dict`) when the
   caller needs typed access; return a scalar (`bool`, `int`) for simple ops
4. Be covered by a test in `tests/test_store.py` that uses the shared `store`
   fixture (real Postgres, no mocks)

```python
# In hyqs/pipeline/store.py — inside class JobStore:

def get_project_by_slug(self, slug: str) -> Project | None:
    with self._pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = %s", (slug,)
        ).fetchone()
    return Project.from_row(row) if row else None
```

```python
# In tests/test_store.py:

def test_get_project_by_slug_returns_none_when_missing(store):
    assert store.get_project_by_slug("nonexistent") is None

def test_get_project_by_slug_returns_project(store):
    p = store.create_project("My Project", "/tmp/repo")
    assert store.get_project_by_slug(p.slug) == p
```

---

## 4. Adding a Web Route

Web routes live in `hyqs/web/app.py`. The pattern is:

1. Write an `async def` handler that accepts a `Request` and returns a
   `JSONResponse` (or `Response` for non-JSON)
2. Register it via a `Route(...)` entry in the list inside `build_app()`
3. Use `request.state.store` to reach the `JobStore`; never import `store`
   directly
4. For mutations, validate input with `await request.json()` and raise
   `HTTPException` for bad input

```python
# Handler
async def get_project(request: Request) -> JSONResponse:
    project_id = int(request.path_params["id"])
    project = request.state.store.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(project_to_dict(project))

# Registration inside build_app():
Route("/api/projects/{id:int}", get_project),
```

---

## 5. Adding an MCP Tool

MCP tools live in `hyqs/tools/` as `@server.tool()`-decorated async functions.
Rules:

1. Decorate with `@server.tool()` (the MCP server instance in scope)
2. Accept only JSON-serialisable parameters; annotate every param and return
3. Return a plain `str` or `dict`; never raise — instead return an error string
4. Keep side effects minimal: read data from the store, write nothing unless the
   tool name makes mutation explicit

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP("hyqs-memory")

@server.tool()
async def list_facts() -> list[str]:
    """Return all stored memory facts."""
    rows = store.list_facts()
    return [row["text"] for row in rows]

@server.tool()
async def add_fact(text: str) -> str:
    """Persist a new fact and return its ID."""
    fact_id = store.add_fact(text)
    return f"saved fact #{fact_id}"
```

---

## 6. Adding a Pipeline Stage

Pipeline stages live in `hyqs/pipeline/stages.py`. Each stage has:

1. A `<NAME>_SYS` string constant — the append-to-system prompt
2. An `async def <name>(backend, ...) -> tuple[<result>, Usage]` function
3. For structured stages (plan, review): call `contracts.parse_and_validate`
4. For free-form stages (build, fix): return `run.text` directly
5. Never commit inside a stage — the pipeline layer does that

```python
SUMMARIZE_SYS = """\
You are the SUMMARIZE stage. Read the diff and produce a one-paragraph summary.
End with a sentinel block using contracts.RESULT_START_MARKER / RESULT_END_MARKER,
followed by a JSON object matching the stage schema: {\"summary\": str}."""


async def summarize(
    backend: AgentBackend, worktree: str, base: str, timeout: int | None = None,
) -> tuple[dict, Usage]:
    prompt = (
        f"Summarize the diff of this branch against '{base}'.\n\n"
        "Produce the summary now."
    )
    run = await backend.run(
        prompt=prompt, cwd=worktree, role=Role.REVIEWER,
        append_system=SUMMARIZE_SYS, timeout=timeout,
    )
    try:
        data = contracts.parse_and_validate("summarize", run.text)
    except contracts.ContractError as e:
        raise ValueError(str(e)) from e
    return data, run.usage
```

---

## 7. Test Layout

- One test file per area: `tests/test_<area>.py`
- Shared fixtures in `tests/conftest.py` only; never define fixtures inside a
  test file that could be shared
- No mocking of the database — use the `store` fixture (real Postgres)
- Mock only external I/O that cannot run in CI: git subprocess calls, HTTP,
  filesystem writes outside `tmp_path`
- Test names: `test_<what>_<expected_behaviour>` — no redundant "test" prefix
  inside the name after the first word

```python
# tests/test_store.py

def test_create_project_returns_project_with_id(store):
    p = store.create_project("Demo", "/tmp/demo")
    assert p.id > 0
    assert p.name == "Demo"


def test_get_project_returns_none_for_unknown(store):
    assert store.get_project(999) is None
```

```python
# tests/test_stages_conventions.py — no Postgres needed, mock the backend

from unittest.mock import AsyncMock, MagicMock
from hyqs.pipeline import stages
from hyqs.pipeline.models import Usage

def _make_backend(text: str = "") -> MagicMock:
    result = MagicMock()
    result.text = text
    result.usage = Usage()
    backend = MagicMock()
    backend.run = AsyncMock(return_value=result)
    return backend
```

---

## 8. Frontend Live-Data Hooks

When two or more views poll the same live backend feed (SSE stream + its
initial-fetch fallback), pull the fetch/subscribe logic into one shared hook
under `hyqs/web/frontend/src/hooks/` instead of duplicating a `useEffect`
pair per view. The hook must:

1. Open every stream/fetch it owns together, on one tick, so views reading
   multiple related datasets (e.g. workers + supervisor) never render
   numbers from two independently-timed polls
2. Never swallow a fetch/stream error into `.catch(() => {})` — surface it as
   explicit state (e.g. `error`, `forbidden`) instead of leaving the caller's
   loading state (a spinner or "Connecting…") stuck forever
3. Distinguish a `"forbidden"` error (`e.message === "forbidden"`) from a
   generic connection failure, so the view can show a no-access message
   instead of an endlessly retrying spinner
4. Expose a `retry()` that closes and reopens every stream it owns
5. Be covered by a test in `hooks/<name>.test.js` using `renderHook` from
   `@testing-library/react`, mocking only the underlying API functions

Views consuming the hook must render three states: forbidden → access
message, error → message + Retry button, no data yet → the existing loading
UI. See `hyqs/web/frontend/src/hooks/useFleetData.js` and its callers
(`AdminCommandCenter.jsx`, `WorkLanes.jsx`).

---

## 9. Web UI information architecture

Ratified 2026-07-14. These rules are extend-only — never weaken or drop one
to make a feature fit.

1. Organize surfaces by the question they answer, one surface per question:
   `Overview` = how is the project, `Plan` = what should we build,
   `Work` = what is happening, `History` = what shipped/was decided,
   `Analytics` = cost + pipeline metrics. Don't fold a second question into
   an existing surface — give it its own tab instead.
2. Navigation lives in `constants.js` registries (`WORKSPACE_TABS`,
   `PLAN_TABS`, `ADMIN_TABS`, …) — never hardcode a tab list inside a
   component. A component reads the registry and renders it; it does not
   own it.
3. Every default view must show data: order it attention-first (failures
   and in-progress work before settled/done items) and never default to an
   empty state on a page that has records.
4. Destructive actions (archive, delete, restore) are small ghost buttons
   tucked behind a menu or a `window.confirm(...)`, never the most
   prominent control on a row or surface. For an irreversible, high-blast-
   radius action (deleting a whole project, not a single row), a
   `window.confirm(...)` isn't enough friction — require the user to type
   the exact resource name into an inline confirm before the destructive
   button enables. Canonical example: `WorkspaceSettings.jsx`'s "Delete
   project" — a `.btn-ghost-danger` button reveals a type-the-project-name
   input, and the real delete button (`.btn-danger`) stays `disabled` until
   the typed value equals `project.name`.
5. Long text (job ideas, diagnoses, descriptions) renders as a title plus an
   expander — never dumped inline in a list row.
6. Ratified 2026-07-14 (job #686). `Work` is organized as attention-first
   status lanes, in this fixed order: Needs Attention → Running Now →
   Queued → Recently Finished. Runtime's former worker-presence and
   dead-letter/needs-judgment surfaces live inside those lanes (see
   `WorkLanes.jsx`) — a standalone Runtime tab must not be reintroduced.
7. Ratified 2026-07-14 (job #687). `History` is organized as sub-tabs
   Changelog (releases grouped by calendar day, one-line expandable rows —
   see `HistoryTab.jsx`/`ChangelogTab.jsx`) and Decisions (list+expand).
   `Analytics` is a single unified project-scoped view (stat row +
   cost/throughput trend charts + collapsible detail tables — see
   `AnalyticsTab.jsx`). Standalone Insights, Performance, Changelog, or
   Decisions top-level tabs must not be reintroduced, and Analytics must
   not be re-split back into Cost/Pipeline sub-tabs.
8. Ratified 2026-07-14 (job #688). The admin nav is 5 items: Command
   Center / Projects / New Project / Providers / Access. Command Center
   (`AdminCommandCenter.jsx`) merges the old Overview + Fleet pages into
   one platform-wide execution-health page, using the same Needs Attention
   → Running Now → Queued → Recently Finished lane structure as the
   project Work page (rule 6) — one altitude up. Access
   (`AdminAccess.jsx`) groups Roles, Invitations, Users, and Audit as
   sub-tabs (`ACCESS_TABS` in `constants.js`), each still gated by its own
   permission. Standalone Overview, Fleet, Roles, Invitations, Users, or
   Audit top-level tabs must not be reintroduced. The global admin-nav page
   `Usage & Cost` (`AdminUsageCost.jsx`, epic 101) is another instance of
   this global-admin-mirrors-per-project-lane pattern: it holds the
   cross-project cost rollup plus a filed-by/channel breakdown, parallel to
   how Command Center mirrors the per-project Work lanes.

```jsx
// Good — registry-driven sub-tabs, no hardcoded list in the component
import { PLAN_TABS, PLAN_TAB_LABEL } from "../constants.js";

{PLAN_TABS.map((t) => (
  <button key={t} onClick={() => onNavWorkspace(project.id, "plan", t)}>
    {PLAN_TAB_LABEL[t]}
  </button>
))}

// Bad — tab list invented inline, drifts from the route parser
{["epics", "backlog"].map((t) => <button key={t}>{t}</button>)}
```

```jsx
// Good — destructive action behind a menu + confirm (see EpicsIndex.jsx)
<button onClick={() => setMenuOpen(true)}>⋯</button>
{menuOpen && (
  <button onClick={() => window.confirm(`Archive "${epic.name}"?`) && archiveEpic(epic.id)}>
    Archive
  </button>
)}

// Bad — the loudest, most prominent control on the row
<button className="cancel-btn-huge">ARCHIVE</button>
```

```jsx
// Good — irreversible, high-blast-radius delete behind a type-to-confirm
// (see WorkspaceSettings.jsx)
{!confirmOpen ? (
  <button className="btn-ghost-danger" onClick={() => setConfirmOpen(true)}>
    Delete project
  </button>
) : (
  <>
    <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} />
    <button
      className="btn btn-danger"
      disabled={confirmText !== project.name}
      onClick={handleDelete}
    >
      Confirm delete
    </button>
  </>
)}

// Bad — a full-width red banner is the most prominent element on the page,
// and window.confirm() is the only guard against a typo-click
<button className="btn btn-danger" style={{ width: "100%" }} onClick={handleDelete}>
  Delete project
</button>
```

---

## 10. Visual design system

Codified 2026-07-13. Like §9, these rules are extend-only — never weaken or drop
one to make a screen fit. The system already exists: the tokens live in
`src/tokens.css` and the component classes in `src/styles.css`. These rules govern
how to USE it so every screen reads as one coherent, polished system.

1. **Tokens are the single source of truth.** Every color, space, radius, shadow,
   font-size, duration, and z-index in a component comes from a `tokens.css`
   variable — never a raw hex or px literal in JSX inline styles or a new CSS rule
   (the only px literals allowed are breakpoint values in `@media`, see rule 3).
   Canonical color tokens are `--bg / --panel / --line / --txt / --dim / --accent /
   --ok / --bad / --run`; the `--color-*` names are thin aliases — consume either,
   but never redefine the canonical set, only alias it. Every color must resolve in
   BOTH themes: when you add one, add its `[data-theme="light"]` override too.
   `--oauth-google-bg` / `--oauth-google-color` / `--oauth-google-border` and
   `--oauth-apple-bg` / `--oauth-apple-color` / `--oauth-apple-border` are the one
   documented exception to the per-theme-override rule: they're fixed brand colors
   (Google/Apple sign-in button styling), not app-theme colors, so their light-theme
   "override" is intentionally identical to `:root`.

2. **Use the scales, not ad-hoc numbers:** spacing `--space-1..10`, type
   `--text-xs..2xl` plus `--text-3xl` (32px, for icon glyph sizes like
   `.empty-icon` — not body text), radius `--radius-sm|md|lg|full`, elevation
   `--shadow-sm|md|lg`, motion `--dur-fast|base` + `--ease`, z-index
   `--z-dropdown|nav-scrim|nav|nav-toggle|drawer|modal|popover|toast` (ascending:
   dropdown 100 < nav-scrim 199 < nav 200 < nav-toggle 201 < drawer 300 < modal 400
   < popover 500 < toast 9999 — nav-scrim/nav-toggle sit either side of nav because
   the hamburger toggle button must stay above the scrim it opens/closes). Snap to
   the nearest existing step; don't invent new in-between values.

3. **Responsive:** the canonical breakpoints are `--bp-sm` (600px) and `--bp-md`
   (900px). `@media` rules must use those literal px values (this repo has no
   PostCSS custom-media plugin) — do NOT introduce new breakpoints; 480/700/767 are
   legacy, migrate them toward 600/900 when you touch that code. Always-true rules:
   wide content (tables, DAG, fleet grids) scrolls inside its OWN container so the
   page body never scrolls horizontally; interactive targets are at least
   `--tap-min` (44px); and no content region is trapped in a fixed-viewport-height
   inner-scroll box on desktop — let it flow with the page (a `max-height:Nvh;
   overflow:auto` panel, if used at all, is gated to mobile).

4. **Compose the canonical shared component; never a one-off restyle.** There is
   exactly one of each — use it:
   - Page title/subtitle → `PageHeader.jsx`
   - Empty state → `EmptyState.jsx`; loading → `Spinner.jsx`; the
     forbidden/error/loading trio per §8 → `PageState.jsx` (paired with the
     `usePageData.js` hook for a single fetch — see `useFleetData.js` for the
     multi-stream case)
   - Job lists → `JobTable.jsx` / `JobCard.jsx`; long text (ideas, diagnoses) →
     `MarkdownContent.jsx` inside a title+expander (§9.5); day-grouped feeds
     (changelog/audit/remediation-style timelines) → `FeedList.jsx`
   - Buttons → `.btn-secondary` / `.btn-danger` / `.btn-ghost-danger` (destructive
     friction per §9.4); badges → `.badge*`; cards → `.card*`; nav chrome →
     `Sidebar.jsx` / `BottomTabBar.jsx`; toasts → `Toast.jsx`
   Need something new? Add ONE shared component under `src/components/` (plus a class
   in `styles.css`) and reuse it — never a bespoke inline-styled block per screen.

5. **Accessibility baseline:** interactive elements show `--focus-ring` on
   `:focus-visible`; text and meaningful UI meet WCAG AA contrast in BOTH themes;
   every input has a label; every action is keyboard-reachable.

6. **Icon vocabulary.** `src/components/icons.js` is the single source of truth for
   icon choices that are reused across screens (e.g. `IDEATE_ICON` = `Sparkles`,
   `ARCHITECT_ICON` = `DraftingCompass`, both from `lucide-react`) — per rule 4,
   "add ONE shared component … never a bespoke inline-styled block per screen"
   applies to icon choices too: don't let two screens pick different glyphs for the
   same concept. Prefer `lucide-react` components over emoji glyphs in shared
   components going forward; existing emoji in per-screen (non-shared) code is not
   required to migrate in the same pass. `EmptyState.jsx`'s `icon` prop accepts
   either a string emoji (legacy) or a `lucide-react` icon component.

```jsx
// Good — tokens + a shared component
<div style={{ padding: "var(--space-4)", gap: "var(--space-2)" }}>
  <span className="badge badge-feature">Feature</span>
</div>

// Bad — raw literals and a bespoke inline-styled control
<div style={{ padding: "15px", gap: "7px" }}>
  <span style={{ background: "#5b8cff", borderRadius: "6px", padding: "2px 8px" }}>
    Feature
  </span>
</div>
```
