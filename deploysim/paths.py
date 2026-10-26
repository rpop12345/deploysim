"""Locations of tools, schemas and local state, relative to the project root."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Paths:
    """root is $DEPLOYSIM_ROOT, or the current directory. Outside a checkout, tools come from
    PATH, and the schemas and CRDs from the installed package."""

    root: Path

    @classmethod
    def from_env(cls) -> Paths:
        return cls(Path(os.environ.get("DEPLOYSIM_ROOT") or Path.cwd()).resolve())

    @property
    def kubectl(self) -> Path:
        return self._tool("kubectl")

    @property
    def kubectl_validate(self) -> Path:
        return self._tool("kubectl-validate")

    @property
    def schemas(self) -> Path:
        return self._data("schemas")

    @property
    def crds(self) -> Path:
        return self._data("crds")

    @property
    def state_dir(self) -> Path:
        return self.root / ".deploysim"

    @property
    def audit_log(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def kubeconfig(self) -> Path:
        return self.state_dir / "kubeconfig"

    def _tool(self, name: str) -> Path:
        pinned = self.root / "bin" / name
        found = None if pinned.exists() else shutil.which(name)
        return Path(found) if found else pinned

    def _data(self, name: str) -> Path:
        local, packaged = self.root / name, PACKAGE / name
        return packaged if not local.exists() and packaged.exists() else local
