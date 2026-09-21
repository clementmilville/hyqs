#!/usr/bin/env python3
"""Build the public "how it works" site.

Sources live in docs/site (this folder): one body fragment per chapter under
pages/<slug>.body.html, an optional pages/<slug>.js, and shared assets under
assets/. Output is written to hyqs/web/frontend/public/how-it-works/, which
Vite copies verbatim into the console build, so the pages are served without
login at /how-it-works/<slug>/.

The build adds three things every chapter shares: a one-sentence takeaway per
section (TAKES), a story strip that follows one real job (STORY), and a
"deep" marker on dense blocks so the Skim/Deep reading modes and the deck
mode can fold them away without removing anything.

Run:  python3 docs/site/build.py
"""

from __future__ import annotations

import html
import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = ROOT / "hyqs/web/frontend/public/how-it-works"
BASE = "/how-it-works/"

# Byline for the generated site. Unset by default so a fork publishes without
# inheriting somebody else's identity; each part is omitted when empty.
AUTHOR = os.environ.get("HYQS_SITE_AUTHOR", "")
EMAIL = os.environ.get("HYQS_SITE_EMAIL", "")
LINKEDIN = os.environ.get("HYQS_SITE_LINKEDIN", "")
FIGURES_DATE = "6 September 2026"

# slug, nav label, title, question, gallery description
CHAPTERS = [
    (
        "",
        "Cover",
        "Hyqs",
        "",
        "Hyqs: the autonomous SDLC pipeline that plans, builds, tests, reviews, secures, merges and deploys on its own, and leaves evidence at every step. Production grade for regulated industries.",
    ),
    (
        "pipeline",
        "Pipeline",
        "The life of a job",
        "What happens to a change between the request and production?",
        "Every stage, gate, budget and fix path a Hyqs job goes through, with a clickable flow and a step-by-step simulation.",
    ),
    (
        "workers",
        "Workers",
        "Workers and the control plane",
        "What runs the pipeline, and how do you size and configure it?",
        "Executors, the elected supervisor and the host-pinned deployer: topology, scaling, the per-project agent roster and tool policies.",
    ),
    (
        "intake",
        "Intake",
        "Getting work in",
        "How does work enter the system, and how do people steer it?",
        "The five ways a job is filed, the intake interview, ideation and the architect, MCP, the CLI and Slack threads.",
    ),
    (
        "console",
        "Console",
        "The console",
        "What does the team see day to day?",
        "A screen-by-screen tour of the Hyqs console: work lanes, job detail, command center, plan, analytics, runtime and access.",
    ),
    (
        "evidence",
        "Evidence",
        "Evidence and governance",
        "What do we hand the auditor?",
        "Every record a job leaves, the database-level audit trail, roles and permissions, and the questions an auditor can ask.",
    ),
    (
        "economics",
        "Economics",
        "Economics",
        "What does a change cost, and where do the tokens go?",
        "The cost ledger, token mix, rework share, a cost calculator and the economics report's own audit.",
    ),
    (
        "deploy",
        "Deploy",
        "Deployment and operations",
        "How does a change reach production and stay there?",
        "Project provisioning, deploy modes and gates, probe-first cutover, environments and promotions, signing, secrets and runtime operations.",
    ),
    (
        "regulated",
        "Regulated",
        "For regulated industries",
        "Why does this fit a bank?",
        "The controls a risk function asks for, mapped to the mechanisms that provide them, with the limits stated first.",
    ),
    (
        "reference",
        "Reference",
        "Reference",
        "Every setting, tool, key, price and permission, in one place.",
        "Lookup tables for the Hyqs pipeline: settings explorer, tool policies, MCP catalogue, deploy configuration, prices and the permission matrix.",
    ),
]

# One takeaway sentence per section, keyed "slug#section-id". Shown in Skim mode and on slides.
TAKES = {
    "pipeline#flow": "Eleven steps. The deterministic ones decide, the AI ones propose, and every failure has a named exit.",
    "pipeline#gates": "A job is judged on the files it changed, never on the repository's old debt. That is why the fix loop converges.",
    "pipeline#heal": "Every retry has its own budget, and none of them can spend another's.",
    "workers#topology": "Three worker types and one database. Nothing is elected but the supervisor; nothing is pinned but the deployer.",
    "workers#scale": "Slots times processes times hosts, then capped by a roster you edit in the console.",
    "workers#roster": "A stronger model for review and a cheaper one for build is two rows, not a code change.",
    "workers#roles": "Reviewers cannot write. The SDK removes the tools and the gate guard reverts anything that slips through.",
    "workers#supervisor": "One elected, AI-free loop every 120 seconds, every action capped and recorded.",
    "workers#liveness": "Alive means a fresh heartbeat row, not an active unit.",
    "intake#sources": "Five doors, one rule: every job knows who filed it and how.",
    "intake#interview": "The interviewer talks; a nine-field check in code decides when a project is ready.",
    "intake#ideate": "The model proposes a job graph, a person clicks to file, and the queue takes no advice from either.",
    "intake#mcp": "Claude.ai, Claude Code or a script drive the same API under the same permissions as the console.",
    "intake#notify": "One Slack thread per job, from planning to deployed, with every detour named.",
    "console#tour": "Attention first: what needs a person sits at the top of every screen.",
    "console#design": "State is encoded, not decorated: badges, chips, colours and the same stage emoji as Slack.",
    "evidence#records": "Nine records per job, each with one writer and one moment, and the table says which are pruned.",
    "evidence#audit": "A trigger inside the database, with three columns the application cannot forge.",
    "evidence#identity": "Invite-only sign-in, eight roles, twenty-six permissions, editable at runtime.",
    "evidence#auditor": "Fifteen questions an auditor asks, each answered by a query rather than a reconstruction.",
    "economics#headline": "A shipped, reviewed, scanned and deployed change for about two minutes of an engineer.",
    "economics#calc": "Put in your volume and your rate; the defaults are the ledger's.",
    "economics#ledger": "Mostly cache reads. Context is what costs, not output.",
    "economics#quality": "Rework is 19.5 percent of tokens, measured per stage, and it is the number we work on.",
    "economics#selfaudit": "Two ledgers disagree by six percent, and the report says so before anyone asks.",
    "deploy#projects": "A provisioned project deploys on its first day, with loopback ports and dropped capabilities from the first commit.",
    "deploy#modes": "Five modes, one question: did it come live. A no is a failed job, never a rollback.",
    "deploy#probe": "The last-good container keeps serving until the new one proves itself three times.",
    "deploy#promotion": "Build once, promote by digest. Push to your environments, offer to a client's.",
    "deploy#supply": "The reviewed commit, the signed digest and the deployed digest are the same artefact.",
    "deploy#ops": "Deployed and serving are two different signals, and the runtime screen shows both.",
    "regulated#limits": "Four limits we state before you find them.",
    "regulated#bank": "Eight controls a risk function asks for, each mapped to a mechanism and a chapter.",
    "reference#settings": "Every environment variable with its default.",
    "reference#roles": "The exact tool list per agent role.",
    "reference#mcp": "Thirty-eight tools and their permissions.",
    "reference#deployconfig": "Every deployment key.",
    "reference#pricing": "The price table.",
    "reference#matrix": "Roles by permissions.",
}

# The story spine: one real job, #4244, followed across chapters.
STORY_STEPS = [
    ("filed", "Filed", "19:42"),
    ("claimed", "Claimed", "19:59"),
    ("plan", "Plan", "2 min"),
    ("build", "Build", "9 min"),
    ("lint", "Lint", "5 s"),
    ("test", "Test", "11 s"),
    ("review", "Review", "pass"),
    ("security", "Security", "✗ stop"),
    ("fix", "Fix", "2 min"),
    ("verify", "Re-verify", "✓ ✓ ✓"),
    ("merge", "Merge", "20:18"),
    ("deploy", "Deploy", "20:19"),
    ("live", "Live", "20:19:53"),
]
STORY = {
    "": ("", ""),
    "pipeline": (
        "plan build lint test review security fix verify merge deploy",
        "#4244 walked every step on this page. Security said no at 20:15: the rate limiter was keyed on a header the caller controls and never evicted. One fix round, a re-run of lint, test, review and security, then merge. Total wall clock 37 minutes, 16 of them waiting in the queue.",
    ),
    "workers": (
        "claimed deploy",
        "Slot 0 of the single eight-slot process claimed #4244 at 19:59:24 with the roster's Coder (Sonnet 5) agent and held it through the fix. The deploy step was claimed by slot 7 at 20:19:07. Both claims are audit rows with the slot id as actor.",
    ),
    "intake": (
        "filed",
        "#4244 came through the MCP door at 19:42:55, filed from a Claude session on the owner's behalf with an idempotency key and a remediation-safe title. It waited 16 minutes behind other work before a slot was free.",
    ),
    "console": (
        "build security fix",
        "Every screen in this chapter can be opened on #4244: its Stages tab shows the security stop and the fix round, its Cost tab the seven model calls, its Diff tab the rate limiter before and after.",
    ),
    "evidence": (
        "plan build review security fix merge deploy",
        "#4244 left 21 stage events, 7 usage rows, 1 deploy row, 1 decision record in the repository, and an audit row for every status change, each stamped with the worker slot that made it.",
    ),
    "economics": (
        "plan build review security fix verify",
        "#4244 cost $11.01 and 9.95 million tokens, above the median because the build alone read 5.6 million cached tokens. The security stop cost $0.76 to find, $1.28 to fix and $1.10 to re-verify: the rework line, on one job.",
    ),
    "deploy": (
        "merge deploy live",
        "Squash-merged at 20:18:52. The deploy claimed at 20:19:07 ran the platform's own blue-green release, proved the new console generation by PID, and recorded commit cac2c0e as live at 20:19:53.",
    ),
    "regulated": (
        "build review security merge deploy",
        "Written by slot 0's coder inside a worktree, judged by read-only reviewers, stopped by one of them, landed by the runner under the merge lock, shipped by slot 7 with a health gate. Four principals, four tool sets, one evidence trail.",
    ),
    "reference": ("", ""),
}

DEEP_BLOCKS = (
    "tbl", "grid2", "grid3", "card", "callout", "ctlmap", "honest", "matrix", "qa", "budgets", "catch", "bars",
    "principles", "versus", "ticker", "limits", "band", "ledger",
)


def rail(active: str) -> str:
    items = []
    for i, (slug, label, *_rest) in enumerate(CHAPTERS):
        if i == 0:
            continue
        href = BASE if slug == "" else f"{BASE}{slug}/"
        cls = "chap on" if slug == active else "chap"
        num = "" if i < 2 else f"<b>{i - 1}</b>"
        items.append(f'<a class="{cls}" href="{href}">{num}{html.escape(label)}</a>')
    return (
        '<div class="progress" aria-hidden="true"><i></i></div>'
        '<div class="rail"><div class="wrap">'
        f'<a class="brand" href="{BASE}"><i></i>Hyqs</a>'
        f'<nav aria-label="Chapters">{"".join(items)}</nav>'
        '<div class="ctl" role="group" aria-label="Reading mode">'
        '<button class="mode" data-mode="skim" aria-pressed="true" title="One takeaway per section; open details where you want them">Skim</button>'
        '<button class="mode" data-mode="deep" aria-pressed="false" title="Everything unfolded">Deep</button>'
        f'<a class="present" href="{BASE}deck/" title="A fourteen-slide summary that stands on its own">Deck</a>'
        '<button class="theme" id="themeBtn" title="Switch between light and dark" aria-label="Switch theme">☾</button>'
        "</div></div></div>"
    )


def nextprev(idx: int) -> str:
    parts = []
    if idx > 0:
        s, label, title, *_ = CHAPTERS[idx - 1]
        href = BASE if s == "" else f"{BASE}{s}/"
        parts.append(
            f'<a class="prev" href="{href}"><span class="eyebrow">← Previous</span><b>{html.escape(title)}</b></a>'
        )
    else:
        parts.append("<span></span>")
    if idx < len(CHAPTERS) - 1:
        s, label, title, *_ = CHAPTERS[idx + 1]
        parts.append(
            f'<a class="next" href="{BASE}{s}/"><span class="eyebrow">Next chapter →</span><b>{html.escape(title)}</b></a>'
        )
    return f'<div class="wrap"><div class="nextprev">{"".join(parts)}</div></div>'


def byline_footer() -> str:
    """The deck's footer credit element, or "" when no identity is configured."""
    inner = byline().removeprefix(" · ")
    return f'<p class="s-foot">{inner}</p>' if inner else ""


def author_meta() -> str:
    """The author meta tag, or "" (no empty tag) when no author is configured."""
    return f'<meta name="author" content="{html.escape(AUTHOR)}">\n' if AUTHOR else ""


def byline() -> str:
    """The author credit, or "" when no identity is configured."""
    parts = []
    if AUTHOR:
        parts.append(html.escape(AUTHOR))
    if EMAIL:
        escaped = html.escape(EMAIL)
        parts.append(f'<a href="mailto:{escaped}">{escaped}</a>')
    if LINKEDIN:
        parts.append(
            f'<a href="{html.escape(LINKEDIN)}" rel="me noopener" target="_blank">LinkedIn</a>'
        )
    return (" · " + " · ".join(parts)) if parts else ""


def footer() -> str:
    return (
        '<footer><div class="wrap">'
        f"<span>Hyqs · autonomous SDLC pipeline · figures from the live ledger, "
        f"{FIGURES_DATE}{byline()}</span>"
        "<span>Every mechanism on these pages is cited to a source file in the Hyqs repository.</span>"
        "</div></footer>"
    )


def story_strip(slug: str) -> str:
    lit, text = STORY.get(slug, ("", ""))
    if not text:
        return ""
    lit_set = set(lit.split())
    chips = "".join(
        f'<span class="chip{" on" if k in lit_set else ""}"><b>{html.escape(label)}</b><small>{html.escape(when)}</small></span>'
        for k, label, when in STORY_STEPS
    )
    return (
        '<aside class="story" aria-label="One job, followed across chapters"><div class="wrap">'
        '<div class="story-head"><span class="eyebrow">Follow one change · job #4244</span>'
        f'<a class="small" href="{BASE}console/">see it in the console →</a></div>'
        f'<div class="strip">{chips}</div>'
        f"<p>{text}</p>"
        "</div></aside>"
    )


_SECTION_RE = re.compile(r'<section id="([^"]+)">(.*?)</section>', re.S)
_BLOCK_RE = re.compile(r'^(  <(?:div|figure) class="(' + "|".join(DEEP_BLOCKS) + r')[^"]*")', re.M)


def transform(slug: str, body: str) -> str:
    """Add takeaways, deep markers and per-section detail toggles."""
    n = [0]

    def one(m: re.Match) -> str:
        sid, inner = m.group(1), m.group(2)
        n[0] += 1
        take = TAKES.get(f"{slug}#{sid}", "")
        # mark dense top-level blocks as deep (the reference chapter is all lookup, so nothing folds there)
        if slug != "reference":
            inner = _BLOCK_RE.sub(lambda b: b.group(1) + " data-deep", inner)
        # insert the takeaway and the toggle right after the section header
        head_end = inner.find("  </div>\n", inner.find('<div class="sec-head">'))
        if take and head_end > 0:
            insert = f'\n  <p class="take">{html.escape(take)}</p>'
            if "data-deep" in inner:  # only offer the toggle where it reveals something
                insert += '\n  <button class="details-toggle" type="button" aria-expanded="false">Show the details</button>'
            inner = (
                inner[: head_end + len("  </div>")] + insert + inner[head_end + len("  </div>") :]
            )
        alt = " alt" if n[0] % 2 == 0 else ""
        return f'<section id="{sid}" class="reveal{alt}">{inner}</section>'

    return _SECTION_RE.sub(one, body)


def page(idx: int) -> str:
    slug, label, title, question, description = CHAPTERS[idx]
    body_path = HERE / "pages" / f"{slug or 'index'}.body.html"
    js_path = HERE / "pages" / f"{slug or 'index'}.js"
    body = transform(slug, body_path.read_text())
    body = body.replace("{{BYLINE_FOOTER}}", byline_footer())
    page_js = f"<script>\n{js_path.read_text()}\n</script>" if js_path.exists() else ""
    hero = ""
    if idx > 1:
        hero = (
            '<header class="chapter-hero"><div class="wrap">'
            f'<div class="eyebrow">Chapter {idx - 1} of {len(CHAPTERS) - 2}</div>'
            f'<h1 style="margin-top:12px">{html.escape(title)}</h1>'
            f'<p class="lede">{html.escape(question)}</p>'
            "</div></header>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{author_meta()}<meta name="description" content="{html.escape(description)}">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,300..800&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<link rel="stylesheet" href="{BASE}assets/site.css">
<script>try{{var t=localStorage.getItem("hyqs-site-theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);}}catch(e){{}}</script>
</head>
<body data-read="skim" data-chapter="{slug or "index"}">
{rail(slug)}
{hero}
{story_strip(slug)}
{body}
{nextprev(idx)}
{footer()}
<script src="{BASE}assets/site.js"></script>
{page_js}
</body>
</html>
"""


DECK_TITLE = "The Hyqs deck"
DECK_DESC = (
    "A fourteen-slide summary of Hyqs: what it ships, how AI proposes while deterministic "
    "code decides, the five gates, the evidence it leaves, what a change costs, and the limits."
)


def deck_page() -> str:
    """The deck is a standalone artefact, not a chapter.

    It carries its own compressed copy rather than reusing the reading pages: chapter
    sections run to several hundred words each, which is unreadable projected and worse
    when nobody is narrating. Each slide here stands on its own so the page survives
    being sent as a link.
    """
    body = (HERE / "pages" / "deck.body.html").read_text()
    body = body.replace("{{BYLINE_FOOTER}}", byline_footer())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{author_meta()}<meta name="description" content="{html.escape(DECK_DESC)}">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light dark">
<title>{html.escape(DECK_TITLE)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,300..800&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<link rel="stylesheet" href="{BASE}assets/deck.css">
<script>try{{var t=localStorage.getItem("hyqs-site-theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);}}catch(e){{}}</script>
</head>
<body>
<div class="progress" role="progressbar" aria-label="Deck progress"></div>
<button class="zone prev" type="button" aria-label="Previous slide"></button>
<button class="zone next" type="button" aria-label="Next slide"></button>
<main class="stage">
{body}
</main>
<div class="deck-bar">
  <a class="home" href="{BASE}"><i></i>Hyqs</a>
  <div class="dots" role="group" aria-label="Go to slide"></div>
  <div class="ctl">
    <button id="play" type="button" aria-label="Play the deck on its own">Play</button>
    <button id="prev" type="button" aria-label="Previous slide">&#8592;</button>
    <span class="count" aria-live="polite">1 / 14</span>
    <button id="next" type="button" aria-label="Next slide">&#8594;</button>
    <button id="theme" type="button" aria-label="Switch theme">&#9790;</button>
  </div>
</div>
<script src="{BASE}assets/deck.js"></script>
</body>
</html>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "assets").mkdir(exist_ok=True)
    for f in (HERE / "assets").iterdir():
        shutil.copy2(f, OUT / "assets" / f.name)
    for idx, (slug, *_rest) in enumerate(CHAPTERS):
        dest = OUT / "index.html" if slug == "" else OUT / slug / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page(idx))
        print("wrote", dest.relative_to(ROOT))
    deck_dest = OUT / "deck" / "index.html"
    deck_dest.parent.mkdir(parents=True, exist_ok=True)
    deck_dest.write_text(deck_page())
    print("wrote", deck_dest.relative_to(ROOT))


if __name__ == "__main__":
    main()
