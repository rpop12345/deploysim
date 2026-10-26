from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from deploysim.clock import utcnow
from deploysim.kube import Kubectl as DeploysimKubectl
from deploysim.lease import acquire
from deploysim.paths import Paths
from deploysim.state import State, StateStore, Status
from tests.e2e.support import REPO, CliRun, Kubectl, image, labelled, write_bundle

pytestmark = pytest.mark.e2e

Deploy = Callable[..., CliRun]
CREATED_KINDS = "deployments,services,configmaps,releases.deploysim.io"


def codes(result: CliRun) -> list[str]:
    return [f"{v['source']}:{v['code']}" for v in result.summary["violations"]]


def test_invalid_bundle_creates_nothing(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0", readiness=False), "dev")

    assert result.code == 1
    assert result.summary["outcome"] == "REJECTED"
    assert codes(result) == ["bundle:READINESS_PROBE"]
    assert labelled(kubectl, f"{CREATED_KINDS},leases", app, "dev") == []


def test_promotion_gate(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0"), "staging")

    assert result.code == 1
    assert codes(result) == ["promotion:GATE"]
    assert "never been deployed to dev" in result.summary["violations"][0]["message"]
    assert labelled(kubectl, CREATED_KINDS, app, "staging") == []


def test_promotion_gate_needs_same_bundle(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", replicas=2), "dev")

    result = deploy(write_bundle(tmp_path, app, "1.0.0", replicas=3), "staging")

    assert result.code == 1
    assert codes(result) == ["promotion:GATE"]
    assert labelled(kubectl, CREATED_KINDS, app, "staging") == []


def test_version_reused_different_image(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", image_seed="a"), "dev")

    result = deploy(write_bundle(tmp_path, app, "1.0.0", image_seed="b"), "dev")

    assert result.code == 1
    assert "immutability:RELEASE_SPEC" in codes(result)
    live = kubectl.get("releases.deploysim.io", f"{app}-1.0.0", "-n", "dev")
    assert live["spec"]["image"] == image(app, "a")


def test_version_reused_different_manifests(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", replicas=2), "dev")

    result = deploy(write_bundle(tmp_path, app, "1.0.0", replicas=3), "dev")

    assert result.code == 1
    assert codes(result) == ["immutability:BUNDLE"]
    assert kubectl.get("deployment", app, "-n", "dev")["spec"]["replicas"] == 2


def test_locked(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    env = DeploysimKubectl.connect(Paths(REPO)).environment("dev")
    acquire(env, app, "someone-else", 60, utcnow())

    result = deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")

    assert result.code == 4
    assert result.summary["outcome"] == "LOCKED"
    assert "held by someone-else" in result.summary["error"]
    assert labelled(kubectl, CREATED_KINDS, app, "dev") == []
    lease = kubectl.get("lease", f"deploysim-{app}", "-n", "dev")
    assert lease["spec"]["holderIdentity"] == "someone-else"


def test_degraded_gate(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    env = DeploysimKubectl.connect(Paths(REPO)).environment("dev")
    store = StateStore(env, app)
    store.save(store.load(), State(app, "dev", status=Status.DEGRADED), utcnow())
    bundle = write_bundle(tmp_path, app, "1.0.0")

    blocked = deploy(bundle, "dev")
    allowed = deploy(bundle, "dev", "--allow-degraded")

    assert blocked.code == 1
    assert codes(blocked) == ["environment:DEGRADED"]
    assert allowed.code == 0, allowed.stderr
    assert allowed.summary["environment_status"] == "HEALTHY"
