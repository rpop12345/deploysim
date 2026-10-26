from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from deploysim.audit import AuditLog
from tests.e2e.support import (
    NAME_LABEL,
    CliRun,
    Kubectl,
    Manifest,
    delete_app,
    deploysim,
    image,
    labelled,
    state_of,
    wait_for,
    write_bundle,
)

pytestmark = pytest.mark.e2e

Deploy = Callable[..., CliRun]
FAST = {"progress_deadline": 5, "timeout": 15}
LEASE = "3"
STAGE_AT = {"after-prepare": "PREPARE", "after-deploy": "DEPLOY", "during-rollback": "ROLLBACK"}


def wait_for_lease_expiry() -> None:
    time.sleep(int(LEASE) + 1)


def phase(kubectl: Kubectl, app: str, version: str) -> str:
    release = kubectl.get("releases.deploysim.io", f"{app}-{version}", "-n", "dev")
    return str((release.get("status") or {}).get("phase"))


@dataclass
class Crash:
    point: str
    app: str
    audit_log: Path
    crashed: CliRun
    state_after_crash: Manifest
    while_locked: CliRun
    recovered: CliRun
    state_after_recovery: Manifest


@pytest.fixture(scope="module", params=list(STAGE_AT))
def crash(
    request: pytest.FixtureRequest, kubectl: Kubectl, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Crash]:
    """1.0.0 deployed; 1.1.0 crashes at the point; a deploy at once; a deploy after expiry."""
    point = request.param
    app = f"t-{secrets.token_hex(3)}"
    directory = tmp_path_factory.mktemp(app)
    audit = directory / "audit.jsonl"
    first = write_bundle(directory, app, "1.0.0", **FAST)
    assert deploysim("deploy", str(first), "--env", "dev", audit_log=audit).code == 0
    upgrade = write_bundle(directory, app, "1.1.0", **FAST)
    fault = ["--inject-fault", f"crash-{point}", "--lease-seconds", LEASE]

    crashed = deploysim("deploy", str(upgrade), "--env", "dev", *fault, audit_log=audit)
    state_after_crash = state_of(kubectl, app, "dev")
    while_locked = deploysim("deploy", str(upgrade), "--env", "dev", audit_log=audit)
    wait_for_lease_expiry()
    recovered = deploysim("deploy", str(upgrade), "--env", "dev", audit_log=audit)

    yield Crash(
        point=point,
        app=app,
        audit_log=audit,
        crashed=crashed,
        state_after_crash=state_after_crash,
        while_locked=while_locked,
        recovered=recovered,
        state_after_recovery=state_of(kubectl, app, "dev"),
    )
    delete_app(kubectl, app)


def test_crash_leaves_intent(crash: Crash) -> None:
    state, app = crash.state_after_crash, crash.app

    assert crash.crashed.code == 70, crash.crashed.stderr
    assert crash.crashed.summary == {}
    assert "inject_crash" in crash.crashed.stderr
    assert state["status"] == "DEPLOYING"
    assert state["in_flight"]["target"]["release"] == f"{app}-1.1.0"
    assert state["in_flight"]["previous"]["release"] == f"{app}-1.0.0"
    assert state["in_flight"]["stage"] == STAGE_AT[crash.point]


def test_locked_while_lease_live(crash: Crash) -> None:
    assert crash.while_locked.code == 4, crash.while_locked.stderr
    assert crash.while_locked.summary["outcome"] == "LOCKED"


def test_deploy_recovers(kubectl: Kubectl, crash: Crash) -> None:
    summary, app = crash.recovered.summary, crash.app

    assert crash.recovered.code == 0, crash.recovered.stderr
    assert summary["outcome"] == "SUCCEEDED"
    assert summary["attempt"] == 2
    assert summary["recovery"] == {
        "performed": True,
        "interrupted": summary["deployment_id"],
        "to": f"{app}-1.0.0",
        "result": "SUCCEEDED",
    }
    state = crash.state_after_recovery
    assert state["in_flight"] is None
    assert (state["status"], state["current"]["release"]) == ("HEALTHY", f"{app}-1.1.0")
    recovered = [r for r in AuditLog(crash.audit_log).records() if r.stage == "RECOVER"]
    actions = [r.action for r in recovered]
    assert actions[0] == "find_intent"
    assert "record_outcome" in actions
    assert any(r.detail.get("phase") == "Interrupted" for r in recovered)
    assert AuditLog(crash.audit_log).verify() == []


def test_recover_command(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", **FAST)
    deploy(upgrade, "dev", "--inject-fault", "crash-after-deploy", "--lease-seconds", LEASE)
    audit = tmp_path / "recover.jsonl"

    locked = deploysim("recover", "--app", app, "--env", "dev", audit_log=audit)
    wait_for_lease_expiry()
    result = deploysim("recover", "--app", app, "--env", "dev", audit_log=audit)

    assert locked.code == 4
    assert result.code == 0, result.stderr
    assert result.summary["outcome"] == "RECOVERED"
    assert result.summary["recovery"]["to"] == f"{app}-1.0.0"
    state = state_of(kubectl, app, "dev")
    assert (state["status"], state["in_flight"]) == ("HEALTHY", None)
    assert state["current"]["release"] == f"{app}-1.0.0"
    assert state["last_outcome"]["outcome"] == "INTERRUPTED"
    assert phase(kubectl, app, "1.1.0") == "Interrupted"

    def restored() -> bool:
        pods = kubectl.get("pods", "-n", "dev", "-l", f"{NAME_LABEL}={app}")["items"]
        live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        return {c["image"] for p in live for c in p["spec"]["containers"]} == {image(app, "1.0.0")}

    assert wait_for(restored, timeout=10)


def test_recover_first_deploy(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    bundle = write_bundle(tmp_path, app, "1.0.0", **FAST)
    deploy(bundle, "dev", "--inject-fault", "crash-after-deploy", "--lease-seconds", LEASE)
    assert labelled(kubectl, "deployments", app, "dev") == [f"Deployment/{app}"]
    wait_for_lease_expiry()

    result = deploysim("recover", "--app", app, "--env", "dev", audit_log=tmp_path / "r.jsonl")

    assert result.code == 0, result.stderr
    assert result.summary["environment_status"] == "EMPTY"
    state = state_of(kubectl, app, "dev")
    assert (state["status"], state["current"], state["in_flight"]) == ("EMPTY", None, None)
    assert labelled(kubectl, "deployments,services", app, "dev") == []
    assert phase(kubectl, app, "1.0.0") == "Interrupted"


def test_recover_noop(kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0"), "dev")
    configmap = f"deploysim-{app}"
    before = kubectl.get("configmap", configmap, "-n", "dev")["metadata"]["resourceVersion"]

    result = deploysim("recover", "--app", app, "--env", "dev", audit_log=tmp_path / "r.jsonl")

    assert result.code == 0, result.stderr
    assert result.summary["outcome"] == "NO_OP"
    assert result.summary["recovery"]["performed"] is False
    after = kubectl.get("configmap", configmap, "-n", "dev")["metadata"]["resourceVersion"]
    assert after == before


def test_recover_removes_added_objects(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", worker=True, **FAST)
    deploy(upgrade, "dev", "--inject-fault", "crash-after-deploy", "--lease-seconds", LEASE)
    assert f"Deployment/{app}-worker" in labelled(kubectl, "deployments", app, "dev")
    wait_for_lease_expiry()

    result = deploysim("recover", "--app", app, "--env", "dev", audit_log=tmp_path / "r.jsonl")

    assert result.code == 0, result.stderr
    assert result.summary["environment_status"] == "HEALTHY"
    remaining = labelled(kubectl, "deployments,services", app, "dev")
    assert remaining == [f"Deployment/{app}", f"Service/{app}"]


def test_recover_without_stored_bundle(
    kubectl: Kubectl, app: str, tmp_path: Path, deploy: Deploy
) -> None:
    deploy(write_bundle(tmp_path, app, "1.0.0", **FAST), "dev")
    upgrade = write_bundle(tmp_path, app, "1.1.0", **FAST)
    deploy(upgrade, "dev", "--inject-fault", "crash-after-deploy", "--lease-seconds", LEASE)
    kubectl.run("delete", "configmap", f"deploysim-{app}-1.0.0", "-n", "dev")
    wait_for_lease_expiry()
    audit = tmp_path / "recover.jsonl"

    failed = deploysim("recover", "--app", app, "--env", "dev", audit_log=audit)
    again = deploysim("recover", "--app", app, "--env", "dev", audit_log=audit)

    assert failed.code == 3, failed.stderr
    assert failed.summary["outcome"] == "ROLLBACK_FAILED"
    assert failed.summary["recovery"]["result"] == "FAILED"
    assert f"ConfigMap deploysim-{app}-1.0.0 is missing" in failed.summary["error"]
    state = state_of(kubectl, app, "dev")
    assert (state["status"], state["in_flight"]) == ("DEGRADED", None)
    assert state["current"]["release"] == f"{app}-1.1.0"
    assert phase(kubectl, app, "1.1.0") == "Interrupted"
    assert again.code == 0, again.stderr
    assert again.summary["outcome"] == "NO_OP"
