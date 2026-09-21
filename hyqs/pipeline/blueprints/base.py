from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Blueprint(Protocol):
    def scaffold(self, path: Path, name: str, description: str, spec: dict) -> None:
        ...
