"""Locations of tools, schemas and local state, relative to the project root."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """root is $DEPLOYSIM_ROOT, or the current directory."""

    root: Path

    @classmethod
    def from_env(cls) -> Paths:
        return cls(Path(os.environ.get("DEPLOYSIM_ROOT") or Path.cwd()).resolve())

    @property
    def kubectl(self) -> Path:
        return self.root / "bin" / "kubectl"

    @property
    def kubectl_validate(self) -> Path:
        return self.root / "bin" / "kubectl-validate"

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def crds(self) -> Path:
        return self.root / "crds"

    @property
    def state_dir(self) -> Path:
        return self.root / ".deploysim"

    @property
    def audit_log(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def kubeconfig(self) -> Path:
        return self.state_dir / "kubeconfig"
