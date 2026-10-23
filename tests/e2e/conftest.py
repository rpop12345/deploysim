from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest

from tests.e2e.support import ENVIRONMENTS, KUBECONFIG, NAME_LABEL, Kubectl

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
