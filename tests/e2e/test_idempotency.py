from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from deploysim.audit import AuditLog
from tests.e2e.support import (
    CliRun,
    Kubectl,
    Manifest,
    deploysim,
    finish,
    start_deploysim,
    state_of,
    wait_for,
    write_bundle,
)

pytestmark = pytest.mark.e2e

Deploy = Callable[..., CliRun]
FAST = {"progress_deadline": 5, "timeout": 15}
WRITES = {
    "record_intent",
    "store_bundle",
    "record_release",
    "release_phase",
    "apply",
    "record_stage",
    "record_outcome",
    "delete",
}


def snapshot(kubectl: Kubectl, app: str) -> Manifest:
    """resourceVersions and generations a NO_OP must leave untouched."""
    state = kubectl.get("configmap", f"deploysim-{app}", "-n", "dev")
    release = kubectl.get("releases.deploysim.io", f"{app}-1.0.0", "-n", "dev")
    deployment = kubectl.get("deployment", app, "-n", "dev")
    replica_sets = kubectl.get("replicasets", "-n", "dev", "-l", f"app.kubernetes.io/name={app}")
    return {
        "state": state["metadata"]["resourceVersion"],
        "release": release["metadata"]["resourceVersion"],
        "generation": deployment["metadata"]["generation"],
        "replica_sets": sorted(rs["metadata"]["name"] for rs in replica_sets["items"]),
    }


def step(result: CliRun, action: str) -> Manifest:
    steps: list[Manifest] = result.summary["steps"]
    [found] = [s for s in steps if s["action"] == action]
    return found


def test_repeat_is_noop(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    bundle = write_bundle(tmp_path, app, "1.0.0")
    deploy(bundle, "dev")
    before = snapshot(kubectl, app)

    result = deploy(bundle, "dev")

    assert result.code == 0, result.stderr
    assert result.summary["outcome"] == "NO_OP"
    assert result.summary["attempt"] is None
    assert step(result, "desired_state")["result"] == "NO_OP"
    assert {s["action"] for s in result.summary["steps"]}.isdisjoint(WRITES)
    assert snapshot(kubectl, app) == before
    assert state_of(kubectl, app, "dev")["attempts"] == {result.summary["deployment_id"]: 1}


def test_drift_corrected(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    bundle = write_bundle(tmp_path, app, "1.0.0", replicas=2)
    deploy(bundle, "dev")
    kubectl.run("scale", "deployment", app, "-n", "dev", "--replicas=1")

    result = deploy(bundle, "dev")

    assert result.code == 0, result.stderr
    assert result.summary["outcome"] == "SUCCEEDED"
    assert result.summary["drift_corrected"] is True
    assert result.summary["attempt"] == 2
    assert step(result, "desired_state")["detail"]["drift"] == [f"Deployment/{app}"]
    assert kubectl.get("deployment", app, "-n", "dev")["spec"]["replicas"] == 2


def test_retry_after_rollback(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", **FAST)
    failed = deploy(upgrade, "dev", "--inject-fault", "unhealthy")

    retried = deploy(upgrade, "dev")

    assert failed.code == 2
    assert retried.code == 0, retried.stderr
    assert retried.summary["deployment_id"] == failed.summary["deployment_id"]
    assert (failed.summary["attempt"], retried.summary["attempt"]) == (1, 2)
    release = kubectl.get("releases.deploysim.io", f"{app}-1.1.0", "-n", "dev")
    assert (release["status"]["phase"], release["status"]["attempts"]) == ("Succeeded", 2)


def test_concurrent_deploys(kubectl: Kubectl, app: str, tmp_path: Path, audit_log: Path) -> None:
    first = write_bundle(tmp_path, app, "1.0.0")
    second = write_bundle(tmp_path, app, "1.1.0")

    processes = [
        start_deploysim("deploy", str(bundle), "--env", "dev", audit_log=audit_log)
        for bundle in (first, second)
    ]
    results = [finish(process) for process in processes]

    assert sorted(r.code for r in results) == [0, 4], [r.stderr for r in results]
    [winner] = [r for r in results if r.code == 0]
    [loser] = [r for r in results if r.code == 4]
    assert loser.summary["outcome"] == "LOCKED"
    state = state_of(kubectl, app, "dev")
    assert state["in_flight"] is None
    assert state["status"] == "HEALTHY"
    assert state["current"]["release"] == winner.summary["to"]
    assert "holderIdentity" not in kubectl.get("lease", f"deploysim-{app}", "-n", "dev")["spec"]
    assert AuditLog(audit_log).verify() == []


def test_downgrade_flagged(app: str, tmp_path: Path, deploy: Deploy, audit_log: Path) -> None:
    older = write_bundle(tmp_path, app, "1.0.0")
    deploy(older, "dev")
    deploy(write_bundle(tmp_path, app, "1.1.0"), "dev")

    result = deploy(older, "dev")

    assert result.code == 0, result.stderr
    assert step(result, "record_intent")["detail"]["downgrade"] is True
    intents = [r for r in AuditLog(audit_log).records() if r.action == "record_intent"]
    assert [r.detail["downgrade"] for r in intents] == [False, False, True]


def test_lease_renewed_while_verifying(
    kubectl: Kubectl, app: str, tmp_path: Path, audit_log: Path
) -> None:
    """A 2 s Lease outlives itself because VERIFY renews it on every poll."""
    bundle = write_bundle(tmp_path, app, "1.0.0", fault="slow-start")
    args = ["deploy", str(bundle), "--env", "dev"]
    slow = start_deploysim(*args, "--lease-seconds", "2", audit_log=audit_log)

    def verifying() -> bool:
        name = f"releases.deploysim.io/{app}-1.0.0"
        phase = kubectl.run("get", name, "-n", "dev", "-o", "jsonpath={.status.phase}", check=False)
        return phase.stdout == "Verifying"

    wait_for(verifying, timeout=10)
    time.sleep(2.5)
    competing = deploysim(*args, audit_log=audit_log)
    finished = finish(slow)

    assert competing.code == 4, competing.stderr
    assert finished.code == 0, finished.stderr
    assert finished.summary["outcome"] == "SUCCEEDED"
