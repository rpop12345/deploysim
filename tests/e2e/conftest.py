from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e.support import (
    ENVIRONMENTS,
    KUBECONFIG,
    NAME_LABEL,
    CliRun,
    Kubectl,
    deploysim,
)

CLEANUP_KINDS = "deployments,services,configmaps,releases.deploysim.io,leases"


@pytest.fixture(scope="session")
def kubectl() -> Kubectl:
    if not KUBECONFIG.exists():
        pytest.exit(f"{KUBECONFIG} not found: run make cluster-up", returncode=1)
    client = Kubectl()
    if client.run("get", "--raw", "/readyz", check=False).returncode != 0:
        pytest.exit("KWOK cluster is not ready: run make cluster-up", returncode=1)
    return client


@pytest.fixture
def app(kubectl: Kubectl) -> Iterator[str]:
    name = f"t-{secrets.token_hex(3)}"
    yield name
    for namespace in ENVIRONMENTS:
        kubectl.run(
            "delete",
            CLEANUP_KINDS,
            "-n",
            namespace,
            "-l",
            f"{NAME_LABEL}={name}",
            "--ignore-not-found",
            "--wait=false",
        )


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def deploy(audit_log: Path) -> Callable[..., CliRun]:
    def run(bundle: Path, env: str, *extra: str) -> CliRun:
        return deploysim("deploy", str(bundle), "--env", env, *extra, audit_log=audit_log)

    return run
