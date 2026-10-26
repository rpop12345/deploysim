from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.e2e.support import (
    NAME_LABEL,
    CliRun,
    Kubectl,
    Manifest,
    delete_app,
    deploysim,
    finish,
    image,
    labelled,
    start_deploysim,
    state_of,
    wait_for,
    write_bundle,
)

pytestmark = pytest.mark.e2e

Deploy = Callable[..., CliRun]
REPLICAS = 4
MAX_UNAVAILABLE = 1  # Kubernetes default: 25% of 4
FAST = {"progress_deadline": 5, "timeout": 15}


def assert_settled(state: Manifest) -> None:
    assert state["in_flight"] is None
    assert state["status"] in {"HEALTHY", "EMPTY", "DEGRADED"}


def phase(kubectl: Kubectl, app: str, version: str) -> str:
    release = kubectl.get("releases.deploysim.io", f"{app}-{version}", "-n", "dev")
    return str((release.get("status") or {}).get("phase"))


def ready(pod: Manifest) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def live_pods(kubectl: Kubectl, app: str) -> list[Manifest]:
    pods = kubectl.get("pods", "-n", "dev", "-l", f"{NAME_LABEL}={app}")["items"]
    return [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]


@dataclass
class Scenario:
    app: str
    directory: Path
    audit_log: Path
    result: CliRun
    state: Manifest
    target_phase: str
    old_ready_samples: list[int] = field(default_factory=list)
    new_pods_seen: bool = False


@pytest.fixture(scope="module")
def injected_upgrade(
    kubectl: Kubectl, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Scenario]:
    """1.0.0 deployed, then 1.1.0 with --inject-fault unhealthy, sampling pods while it runs."""
    app = f"t-{secrets.token_hex(3)}"
    directory = tmp_path_factory.mktemp(app)
    audit = directory / "audit.jsonl"
    first = write_bundle(directory, app, "1.0.0", replicas=REPLICAS, **FAST)
    assert deploysim("deploy", str(first), "--env", "dev", audit_log=audit).code == 0
    [original] = {pod["metadata"]["labels"]["pod-template-hash"] for pod in live_pods(kubectl, app)}

    upgrade = write_bundle(directory, app, "1.1.0", replicas=REPLICAS, **FAST)
    process = start_deploysim(
        "deploy", str(upgrade), "--env", "dev", "--inject-fault", "unhealthy", audit_log=audit
    )
    samples, new_seen = [], False
    while process.poll() is None:
        pods = live_pods(kubectl, app)
        old = [p for p in pods if p["metadata"]["labels"]["pod-template-hash"] == original]
        samples.append(sum(1 for p in old if ready(p)))
        new_seen = new_seen or len(old) < len(pods)
        time.sleep(0.2)
    result = finish(process)

    yield Scenario(
        app=app,
        directory=directory,
        audit_log=audit,
        result=result,
        state=state_of(kubectl, app, "dev"),
        target_phase=phase(kubectl, app, "1.1.0"),
        old_ready_samples=samples,
        new_pods_seen=new_seen,
    )
    delete_app(kubectl, app)


@pytest.fixture(scope="module")
def failed_rollback(
    kubectl: Kubectl, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Scenario]:
    """1.0.0 deployed, then 1.1.0 with --inject-fault unhealthy-rollback."""
    app = f"t-{secrets.token_hex(3)}"
    directory = tmp_path_factory.mktemp(app)
    audit = directory / "audit.jsonl"
    first = write_bundle(directory, app, "1.0.0", **FAST)
    assert deploysim("deploy", str(first), "--env", "dev", audit_log=audit).code == 0
    upgrade = write_bundle(directory, app, "1.1.0", **FAST)

    args = ["deploy", str(upgrade), "--env", "dev", "--inject-fault", "unhealthy-rollback"]
    result = deploysim(*args, audit_log=audit)

    yield Scenario(
        app=app,
        directory=directory,
        audit_log=audit,
        result=result,
        state=state_of(kubectl, app, "dev"),
        target_phase=phase(kubectl, app, "1.1.0"),
    )
    delete_app(kubectl, app)


def test_unhealthy_upgrade_rolls_back(kubectl: Kubectl, injected_upgrade: Scenario) -> None:
    app, summary = injected_upgrade.app, injected_upgrade.result.summary

    assert injected_upgrade.result.code == 2, injected_upgrade.result.stderr
    assert summary["outcome"] == "ROLLED_BACK"
    assert summary["final"] == summary["from"] == f"{app}-1.0.0"
    assert summary["rollback"] == {"performed": True, "to": f"{app}-1.0.0", "result": "SUCCEEDED"}
    assert summary["environment_status"] == "HEALTHY"
    assert injected_upgrade.target_phase == "RolledBack"
    assert_settled(injected_upgrade.state)
    assert injected_upgrade.state["current"]["release"] == f"{app}-1.0.0"

    def restored() -> bool:
        images = {c["image"] for p in live_pods(kubectl, app) for c in p["spec"]["containers"]}
        return images == {image(app, "1.0.0")}

    assert wait_for(restored, timeout=10)


def test_old_pods_keep_serving(injected_upgrade: Scenario) -> None:
    samples = injected_upgrade.old_ready_samples

    assert injected_upgrade.new_pods_seen, "the faulty ReplicaSet never created pods"
    assert len(samples) > 5
    assert min(samples) >= REPLICAS - MAX_UNAVAILABLE


def test_stored_bundle_unannotated(kubectl: Kubectl, injected_upgrade: Scenario) -> None:
    app = injected_upgrade.app
    stored = kubectl.get("configmap", f"deploysim-{app}-1.1.0", "-n", "dev")
    steps = injected_upgrade.result.summary["steps"]
    [apply] = [s for s in steps if (s["stage"], s["action"]) == ("DEPLOY", "apply")]

    assert "deploysim.io/fault" not in stored["data"]["objects.json"]
    assert apply["detail"]["injected_fault"] == "unhealthy"


def test_broken_build(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")

    result = deploy(write_bundle(tmp_path, app, "1.1.0", fault="unhealthy", **FAST), "dev")

    assert result.code == 2, result.stderr
    assert result.summary["outcome"] == "ROLLED_BACK"
    assert result.summary["final"] == f"{app}-1.0.0"
    state = state_of(kubectl, app, "dev")
    assert_settled(state)
    assert state["status"] == "HEALTHY"
    stored = kubectl.get("configmap", f"deploysim-{app}-1.1.0", "-n", "dev")
    assert "deploysim.io/fault" in stored["data"]["objects.json"]


def test_first_deploy_unhealthy(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0", fault="unhealthy", **FAST), "dev")

    assert result.code == 2, result.stderr
    assert result.summary["outcome"] == "ROLLED_BACK"
    assert result.summary["rollback"] == {"performed": True, "to": None, "result": "SUCCEEDED"}
    assert result.summary["environment_status"] == "EMPTY"
    state = state_of(kubectl, app, "dev")
    assert_settled(state)
    assert (state["status"], state["current"]) == ("EMPTY", None)
    assert labelled(kubectl, "deployments,services", app, "dev") == []
    assert phase(kubectl, app, "1.0.0") == "RolledBack"


def test_rollback_fails(failed_rollback: Scenario) -> None:
    app, summary = failed_rollback.app, failed_rollback.result.summary

    assert failed_rollback.result.code == 3, failed_rollback.result.stderr
    assert summary["outcome"] == "ROLLBACK_FAILED"
    assert summary["rollback"] == {"performed": True, "to": f"{app}-1.0.0", "result": "FAILED"}
    assert summary["environment_status"] == "DEGRADED"
    assert failed_rollback.target_phase == "RollbackFailed"
    assert_settled(failed_rollback.state)
    assert failed_rollback.state["status"] == "DEGRADED"
    assert failed_rollback.state["last_known_good"]["release"] == f"{app}-1.0.0"
    rollback_checks = [
        s for s in summary["steps"] if s["stage"] == "ROLLBACK" and s["action"] == "rollout_status"
    ]
    assert [s["result"] for s in rollback_checks] == ["FAIL"]


def test_degraded_gate(kubectl: Kubectl, failed_rollback: Scenario) -> None:
    app = failed_rollback.app
    fix = write_bundle(failed_rollback.directory, app, "1.1.0", **FAST)

    blocked = deploysim("deploy", str(fix), "--env", "dev", audit_log=failed_rollback.audit_log)
    allowed = deploysim(
        "deploy", str(fix), "--env", "dev", "--allow-degraded", audit_log=failed_rollback.audit_log
    )

    assert blocked.code == 1
    assert [v["code"] for v in blocked.summary["violations"]] == ["DEGRADED"]
    assert allowed.code == 0, allowed.stderr
    state = state_of(kubectl, app, "dev")
    assert_settled(state)
    assert (state["status"], state["current"]["release"]) == ("HEALTHY", f"{app}-1.1.0")
    assert state["last_outcome"]["outcome"] == "SUCCEEDED"


def test_rollback_removes_added_objects(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", worker=True, **FAST)

    result = deploy(upgrade, "dev", "--inject-fault", "unhealthy")

    assert result.code == 2, result.stderr
    assert result.summary["environment_status"] == "HEALTHY"
    deleted = [
        step["detail"]["object"]
        for step in result.summary["steps"]
        if (step["stage"], step["action"]) == ("ROLLBACK", "delete")
    ]
    assert deleted == [f"Deployment/{app}-worker"]
    remaining = labelled(kubectl, "deployments,services", app, "dev")
    assert remaining == [f"Deployment/{app}", f"Service/{app}"]


def test_rollback_without_stored_bundle(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    first = write_bundle(tmp_path, app, "1.0.0", **FAST)
    deploy(first, "dev")
    kubectl.run("delete", "configmap", f"deploysim-{app}-1.0.0", "-n", "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", **FAST)

    failed = deploy(upgrade, "dev", "--inject-fault", "unhealthy")
    degraded = state_of(kubectl, app, "dev")
    redeployed = deploy(first, "dev", "--allow-degraded")

    assert failed.code == 3, failed.stderr
    summary = failed.summary
    assert summary["outcome"] == "ROLLBACK_FAILED"
    assert summary["rollback"] == {"performed": True, "to": f"{app}-1.0.0", "result": "FAILED"}
    assert (summary["final"], summary["environment_status"]) == (f"{app}-1.1.0", "DEGRADED")
    assert f"ConfigMap deploysim-{app}-1.0.0 is missing" in summary["error"]
    assert phase(kubectl, app, "1.1.0") == "RollbackFailed"
    assert_settled(degraded)
    assert (degraded["status"], degraded["current"]["release"]) == ("DEGRADED", f"{app}-1.1.0")
    assert degraded["last_known_good"]["release"] == f"{app}-1.0.0"
    assert redeployed.code == 0, redeployed.stderr
    state = state_of(kubectl, app, "dev")
    assert (state["status"], state["current"]["release"]) == ("HEALTHY", f"{app}-1.0.0")
    assert kubectl.get("configmap", f"deploysim-{app}-1.0.0", "-n", "dev")["immutable"] is True
