"""Deterministic per-project symbol extractor.

Walks a repo's Python and JS/TS files using AST (Python) and regex (JS/TS)
to harvest public symbols — their kind, signature, and one-line docstring.
No AI, no network, no subprocesses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv"}

_HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

_JS_EXPORT_FN = re.compile(r"^export\s+(async\s+)?function\s+(\w+)\s*(\()")
_JS_EXPORT_CLASS = re.compile(r"^export\s+(default\s+)?class\s+(\w+)")
_JS_ROUTER = re.compile(r"\b(?:router|app)\.(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]")


def _dotted_module(repo_path: Path, file_path: Path) -> str:
    rel = file_path.relative_to(repo_path)
    parts = list(rel.parts)
    name = parts[-1]
    if name == "__init__.py":
        parts = parts[:-1]
    elif name.endswith(".py"):
        parts[-1] = name[:-3]
    return ".".join(parts)


def _decorator_names(node: ast.AST) -> list[str]:
    names = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
        elif isinstance(dec, ast.Call):
            inner = dec.func
            if isinstance(inner, ast.Name):
                names.append(inner.id)
            elif isinstance(inner, ast.Attribute):
                names.append(inner.attr)
    return names


def _first_docstring(node: ast.AST) -> str:
    body = getattr(node, "body", [])
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        raw = str(body[0].value.value)
        return raw.strip().splitlines()[0].strip()
    return ""


def _node_signature(source_lines: list[str], node: ast.AST) -> str:
    lineno = getattr(node, "lineno", None)
    if lineno is None or lineno > len(source_lines):
        return ""
    return source_lines[lineno - 1].strip()


def _extract_py(repo_path: Path, file_path: Path) -> list[dict]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    module = _dotted_module(repo_path, file_path)
    results: list[dict] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            results.append({
                "module": module,
                "symbol": node.name,
                "kind": "class",
                "signature": _node_signature(source_lines, node),
                "summary": _first_docstring(node),
            })
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child.name.startswith("_"):
                    continue
                mdecs = _decorator_names(child)
                if any(d in _HTTP_METHODS for d in mdecs):
                    mkind = "route"
                elif "tool" in mdecs:
                    mkind = "tool"
                else:
                    mkind = "method"
                results.append({
                    "module": module,
                    "symbol": f"{node.name}.{child.name}",
                    "kind": mkind,
                    "signature": _node_signature(source_lines, child),
                    "summary": _first_docstring(child),
                })

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            decs = _decorator_names(node)
            if any(d in _HTTP_METHODS for d in decs):
                kind = "route"
            elif "tool" in decs:
                kind = "tool"
            else:
                kind = "function"
            results.append({
                "module": module,
                "symbol": node.name,
                "kind": kind,
                "signature": _node_signature(source_lines, node),
                "summary": _first_docstring(node),
            })

    return results


def _extract_js(repo_path: Path, file_path: Path) -> list[dict]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    module = str(file_path.relative_to(repo_path))
    results: list[dict] = []

    for line in source.splitlines():
        stripped = line.strip()
        m = _JS_EXPORT_FN.match(stripped)
        if m:
            results.append({
                "module": module,
                "symbol": m.group(2),
                "kind": "function",
                "signature": stripped[:200],
                "summary": "",
            })
            continue
        m = _JS_EXPORT_CLASS.match(stripped)
        if m:
            results.append({
                "module": module,
                "symbol": m.group(2) or "default",
                "kind": "class",
                "signature": stripped[:200],
                "summary": "",
            })
            continue
        m = _JS_ROUTER.search(stripped)
        if m:
            results.append({
                "module": module,
                "symbol": m.group(2),
                "kind": "route",
                "signature": stripped[:200],
                "summary": "",
            })

    return results


def extract_symbols(repo_path: str) -> list[dict]:
    """Extract public symbols from all Python and JS/TS files under repo_path.

    Returns a stable list sorted by (module, symbol). No AI, no network,
    no subprocesses — pure AST (Python) + regex (JS/TS).
    """
    root = Path(repo_path)
    if not root.is_dir():
        return []

    results: list[dict] = []

    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        rel_parts = file_path.relative_to(root).parts
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        suffix = file_path.suffix
        if suffix == ".py":
            results.extend(_extract_py(root, file_path))
        elif suffix in {".js", ".ts", ".jsx", ".tsx"}:
            results.extend(_extract_js(root, file_path))

    results.sort(key=lambda r: (r["module"], r["symbol"]))
    return results
