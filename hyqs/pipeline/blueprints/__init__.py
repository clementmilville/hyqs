"""Blueprint registry: named scaffolding templates for new projects."""

from __future__ import annotations

from .bare import BareBlueprint
from .base import Blueprint
from .fastapi import FastAPIBlueprint
from .node import NodeBlueprint
from .python import PythonBlueprint

_REGISTRY: dict[str, Blueprint] = {
    "bare": BareBlueprint(),
    "python": PythonBlueprint(),
    "node": NodeBlueprint(),
    "fastapi": FastAPIBlueprint(),
}

KNOWN_STACKS: list[str] = list(_REGISTRY)


def get_blueprint(stack: str) -> Blueprint:
    try:
        return _REGISTRY[stack]
    except KeyError:
        raise ValueError(f"unknown stack {stack!r}; known: {list(_REGISTRY)}")


def list_blueprints() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["Blueprint", "KNOWN_STACKS", "get_blueprint", "list_blueprints"]
