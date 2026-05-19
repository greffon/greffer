"""Read / write the operator's ``env.env`` file.

Format is shell-style ``KEY="value"`` lines, one per setting, matching
what the greffer service's settings.py expects. The writer is atomic
(write-and-rename via NamedTemporaryFile + os.replace) and sets
``0600`` perms so secrets aren't world-readable.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


_LINE_RE = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"?(.*?)"?\s*$')


@dataclass
class EnvFile:
    """In-memory view of an env.env file. Keys are uppercase env-var names."""

    values: dict[str, str]

    @classmethod
    def empty(cls) -> "EnvFile":
        return cls(values={})

    @classmethod
    def from_text(cls, text: str) -> "EnvFile":
        values: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _LINE_RE.match(stripped)
            if match:
                values[match.group(1)] = match.group(2)
        return cls(values=values)

    @classmethod
    def read(cls, path: Path) -> "EnvFile":
        if not path.exists():
            return cls.empty()
        return cls.from_text(path.read_text(encoding="utf-8"))

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def to_text(self) -> str:
        # Deterministic ordering — sort by key so diffs are clean.
        lines = []
        for key in sorted(self.values):
            value = self.values[key]
            # Wrap in double quotes (matches existing greffer/env.env style)
            # and escape any embedded double quotes.
            escaped = value.replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        return "\n".join(lines) + "\n"

    def write_atomic(self, path: Path) -> None:
        """Write env.env atomically with 0600 perms.

        Uses NamedTemporaryFile + os.replace so a partial write doesn't
        leave a corrupt env.env on disk. The 0600 perms protect any
        secrets (token, etc.) from world-read on shared hosts.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # delete=False so we control the rename; manually unlink on error.
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".env.env.tmp."
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_text())
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.replace(tmp_path, path)
        except Exception:
            # Best-effort cleanup; don't mask the original error.
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise
