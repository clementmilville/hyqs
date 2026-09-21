"""Local secret providers for the deploy host.

Zero-knowledge by design: a provider only reads values that are *already*
sitting on the local deploy host's disk (never the control plane or DB), and
this module never logs or persists a value it reads. v1 ships exactly one
implementation — a file/directory-backed provider — behind a small Protocol
so future providers (Vault, cloud secret managers) can slot in later without
touching call sites.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SecretProvider(Protocol):
    def get(self, name: str) -> str | None:
        ...


class FileSecretProvider:
    """Reads secret values from a local file or directory on the deploy host.

    - ``path`` is a directory: ``get(name)`` reads ``<path>/<name>`` and
      returns its stripped contents, or None if that file doesn't exist.
    - ``path`` is a file: parsed once as KEY=VALUE lines (blank lines and
      lines starting with ``#`` are skipped, surrounding quotes are stripped
      from values); ``get(name)`` looks up ``name`` in that map.
    - ``path`` doesn't exist: ``get`` always returns None.

    Never raises for a missing path or key — this is a read-only,
    best-effort local lookup.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._values: dict[str, str] | None = None
        if self._path.is_file():
            self._values = self._parse_env_file(self._path)

    @staticmethod
    def _parse_env_file(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
        return values

    def get(self, name: str) -> str | None:
        if self._values is not None:
            return self._values.get(name)
        if self._path.is_dir():
            target = self._path / name
            if not target.is_file():
                return None
            return target.read_text().strip()
        return None
