from __future__ import annotations

from pathlib import Path

import pytest

from deploysim import paths
from deploysim.paths import Paths

REPO = Path(__file__).resolve().parents[2]


def test_checkout_preferred() -> None:
    checkout = Paths(REPO)

    assert checkout.kubectl_validate == REPO / "bin" / "kubectl-validate"
    assert checkout.schemas == REPO / "schemas"
    assert checkout.crds == REPO / "crds"


def test_outside_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, package, elsewhere = tmp_path / "tools", tmp_path / "package", tmp_path / "elsewhere"
    for directory in (tools, package / "schemas", package / "crds", elsewhere):
        directory.mkdir(parents=True)
    kubectl = tools / "kubectl"
    kubectl.write_text("#!/bin/sh\n")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(paths, "PACKAGE", package)

    outside = Paths(elsewhere)

    assert outside.kubectl == kubectl
    assert outside.kubectl_validate == elsewhere / "bin" / "kubectl-validate"
    assert outside.schemas == package / "schemas"
    assert outside.crds == package / "crds"
    assert outside.kubeconfig == elsewhere / ".deploysim" / "kubeconfig"
