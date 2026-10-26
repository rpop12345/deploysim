from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from deploysim.audit import AuditLog
from tests.e2e.support import (
    ENVIRONMENTS,
    NAME_LABEL,
    CliRun,
    Kubectl,
    Manifest,
    image,
    state_of,
    wait_for,
    write_bundle,
)

pytestmark = pytest.mark.e2e

Deploy = Callable[..., CliRun]


def release_status(kubectl: Kubectl, app: str, version: str, namespace: str = "dev") -> Manifest:
    release = kubectl.get("releases.deploysim.io", f"{app}-{version}", "-n", namespace)
    status: Manifest = release.get("status") or {}
    return status


def test_first_deploy(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")

    assert result.code == 0, result.stderr
    summary = result.summary
    assert summary["outcome"] == "SUCCEEDED"
    assert summary["attempt"] == 1
    target = f"{app}-1.0.0"
    assert (summary["from"], summary["to"], summary["final"]) == (None, target, target)
    assert summary["environment_status"] == "HEALTHY"

    assert release_status(kubectl, app, "1.0.0")["phase"] == "Succeeded"
    assert release_status(kubectl, app, "1.0.0")["attempts"] == 1

    state = state_of(kubectl, app, "dev")
    assert state["status"] == "HEALTHY"
    assert state["current"] == state["last_known_good"]
    assert state["current"]["release"] == f"{app}-1.0.0"
    assert state["in_flight"] is None
    assert state["attempts"] == {summary["deployment_id"]: 1}

    stored = kubectl.get("configmap", f"deploysim-{app}-1.0.0", "-n", "dev")
    assert stored["immutable"] is True
    assert stored["metadata"]["annotations"]["deploysim.io/bundle-digest"]

    lease = kubectl.get("lease", f"deploysim-{app}", "-n", "dev")
    assert "holderIdentity" not in lease["spec"]

    deployment = kubectl.get("deployment", app, "-n", "dev")
    assert deployment["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "deploysim"


def test_upgrade(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")

    result = deploy(write_bundle(tmp_path, app, "1.1.0"), "dev")

    assert result.code == 0, result.stderr
    assert (result.summary["from"], result.summary["to"]) == (f"{app}-1.0.0", f"{app}-1.1.0")
    assert state_of(kubectl, app, "dev")["last_known_good"]["release"] == f"{app}-1.1.0"

    def running_images() -> set[str] | None:
        pods = kubectl.get("pods", "-n", "dev", "-l", f"{NAME_LABEL}={app}")["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        images = {c["image"] for p in live for c in p["spec"]["containers"]}
        return images if images == {image(app, "1.1.0")} else None

    assert wait_for(running_images, timeout=10) == {image(app, "1.1.0")}


def test_promotion_chain(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    bundle = write_bundle(tmp_path, app, "1.0.0")

    results = [deploy(bundle, env) for env in ENVIRONMENTS]

    assert [r.code for r in results] == [0, 0, 0], [r.stderr for r in results]
    gates = [
        step["result"]
        for r in results
        for step in r.summary["steps"]
        if step["action"] == "promotion_gate"
    ]
    assert gates == ["OK", "OK"]
    for env in ENVIRONMENTS:
        assert release_status(kubectl, app, "1.0.0", env)["phase"] == "Succeeded"


def test_slow_start(app: str, tmp_path: Path, deploy: Deploy) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0", fault="slow-start"), "dev")

    assert result.code == 0, result.stderr
    [verify] = [s for s in result.summary["steps"] if s["action"] == "rollout_status"]
    assert verify["result"] == "OK"
    assert verify["detail"]["waited_s"] >= 4
    assert verify["detail"]["polls"] > 1


def test_audit_trail(app: str, tmp_path: Path, deploy: Deploy, audit_log: Path) -> None:
    result = deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")

    log = AuditLog(audit_log)
    records = log.records()
    assert log.verify() == []
    assert [r.seq for r in records] == list(range(1, len(records) + 1))
    assert [s["seq"] for s in result.summary["steps"]] == [r.seq for r in records]
    stages = [r.stage for r in records]
    first_seen = list(dict.fromkeys(stages))
    assert first_seen == ["VALIDATE", "LOCK", "PREPARE", "DEPLOY", "VERIFY", "COMPLETE"]
    assert stages[-1] == "LOCK"
    for stage in first_seen:
        assert any(r.cmd for r in records if r.stage == stage), f"{stage} has no record with cmd"
    assert {r.deployment_id for r in records} == {result.summary["deployment_id"]}


def test_verify_failure_stops(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")
    broken = write_bundle(
        tmp_path, app, "1.1.0", fault="unhealthy", progress_deadline=5, timeout=15
    )

    result = deploy(broken, "dev")

    assert result.code == 2, result.stderr
    steps = result.summary["steps"]
    [verify] = [s for s in steps if (s["stage"], s["action"]) == ("VERIFY", "rollout_status")]
    assert verify["result"] == "FAIL"
    assert verify["detail"]["reason"] == "ProgressDeadlineExceeded"
    assert state_of(kubectl, app, "dev")["current"]["release"] == f"{app}-1.0.0"
    assert release_status(kubectl, app, "1.1.0")["phase"] != "Succeeded"
