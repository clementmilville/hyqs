"""Idempotent marked-block replacement for CONVENTIONS.md generated inventory.

CONVENTIONS.md holds two kinds of content: curated NORMS (hand-written rules,
guarded by the standing coder rule in ``agents.py`` and the reviewer scrutiny note
in ``stages/review.py``) and generated INVENTORY (what tokens/components/endpoints
exist today — goes stale if hand-maintained). Content between ``BEGIN_MARKER`` and
``END_MARKER`` is INVENTORY: machine-generated, safe to regenerate wholesale.

Projects wire this up via a per-project ``.hyqs/invariants/`` check that extracts
its own inventory and calls :func:`replace_generated_block` to detect staleness.
``CSS_TOKEN_INVARIANT_TEMPLATE`` below is a copy-paste starting point for
CSS-token projects; see ``docs/generated-conventions-blocks.md``.
"""

from __future__ import annotations

import re

BEGIN_MARKER = "<!-- hyqs:generated:begin -->"
END_MARKER = "<!-- hyqs:generated:end -->"

_BLOCK_RE = re.compile(re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)


def replace_generated_block(content: str, generated_text: str) -> str:
    """Replace the marked generated block in *content* with *generated_text*.

    Idempotent: calling this again with the same generated_text is a no-op. If no
    markers are present, the block is appended at the end of the file (creating
    the markers); text before and after the block is otherwise untouched.
    """
    block = f"{BEGIN_MARKER}\n{generated_text.strip()}\n{END_MARKER}"
    if _BLOCK_RE.search(content):
        return _BLOCK_RE.sub(lambda _m: block, content, count=1)
    if content and not content.endswith("\n"):
        content += "\n"
    if content and not content.endswith("\n\n"):
        content += "\n"
    return content + block + "\n"


CSS_TOKEN_INVARIANT_TEMPLATE = '''\
#!/usr/bin/env python3
"""Reference invariant: keep CONVENTIONS.md's generated CSS-token inventory in sync.

Copy this into .hyqs/invariants/check_css_tokens.py and adjust STYLESHEET_PATH /
CONVENTIONS_PATH below for your project. Standalone by design (no hyqs imports):
it runs in an arbitrary project's own checkout, not in the hyqs pipeline process.

Extracts --custom-property tokens and top-level .class selectors from
STYLESHEET_PATH, and keeps them mirrored into the generated block of
CONVENTIONS_PATH between the hyqs:generated markers.
"""
import os
import re
import sys
from pathlib import Path

STYLESHEET_PATH = "styles.css"
CONVENTIONS_PATH = "CONVENTIONS.md"

BEGIN_MARKER = "<!-- hyqs:generated:begin -->"
END_MARKER = "<!-- hyqs:generated:end -->"

_BLOCK_RE = re.compile(re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER), re.DOTALL)
_TOKEN_RE = re.compile(r"(--[A-Za-z0-9_-]+)\\s*:")
_CLASS_RE = re.compile(r"^\\.([A-Za-z0-9_-]+)\\s*\\{", re.MULTILINE)


def extract_tokens_and_classes(css_text):
    tokens = sorted(set(_TOKEN_RE.findall(css_text)))
    classes = sorted(set(_CLASS_RE.findall(css_text)))
    return tokens, classes


def render_block(tokens, classes):
    lines = ["### CSS Custom Properties"]
    lines += [f"- `{t}`" for t in tokens] if tokens else ["- (none)"]
    lines += ["", "### Top-level Classes"]
    lines += [f"- `.{c}`" for c in classes] if classes else ["- (none)"]
    return "\\n".join(lines)


def replace_generated_block(content, generated_text):
    block = f"{BEGIN_MARKER}\\n{generated_text.strip()}\\n{END_MARKER}"
    if _BLOCK_RE.search(content):
        return _BLOCK_RE.sub(lambda _m: block, content, count=1)
    if content and not content.endswith("\\n"):
        content += "\\n"
    if content and not content.endswith("\\n\\n"):
        content += "\\n"
    return content + block + "\\n"


def main() -> int:
    stdin_text = sys.stdin.read()
    changed_raw = stdin_text or os.environ.get("HYQS_CHANGED_FILES", "")
    changed_files = [line.strip() for line in changed_raw.splitlines() if line.strip()]

    if STYLESHEET_PATH not in changed_files:
        return 78  # not applicable: the stylesheet did not change

    css_path = Path(STYLESHEET_PATH)
    if not css_path.is_file():
        return 78

    tokens, classes = extract_tokens_and_classes(css_path.read_text())
    generated_text = render_block(tokens, classes)

    conventions_path = Path(CONVENTIONS_PATH)
    current = conventions_path.read_text() if conventions_path.is_file() else ""
    updated = replace_generated_block(current, generated_text)

    if current == updated:
        return 0  # generated block already matches the stylesheet

    print("CONVENTIONS.md's generated CSS-token inventory is stale. Regenerated block:\\n")
    print(updated)
    return 1  # violation: FIX agent should apply the printed content


if __name__ == "__main__":
    sys.exit(main())
'''
