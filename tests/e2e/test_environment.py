from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from tests.e2e.support import (
    ENVIRONMENTS,
    NAME_LABEL,
    REPO,
    Kubectl,
    Manifest,
    deployment,
    image,
    progress_deadline_exceeded,
    release,
    rollout_complete,
    wait_for,
)

pytestmark = pytest.mark.e2e


def stage_delay_seconds(name: str) -> float:
    stages = yaml.safe_load_all((REPO / "kwok" / "stages.yaml").read_text())
    stage = next(s for s in stages if s["metadata"]["name"] == name)
    return float(stage["spec"]["delay"]["durationMilliseconds"]) / 1000


def pods(kubectl: Kubectl, app: str) -> list[Manifest]:
    items: list[Manifest] = kubectl.get("pods", "-n", "dev", "-l", f"{NAME_LABEL}={app}")["items"]
    return items


def ready(pod: Manifest) -> bool:
    conditions = pod.get("status", {}).get("conditions", [])
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def test_nodes_ready(kubectl: Kubectl) -> None:
    nodes = kubectl.get("nodes")["items"]

    assert len(nodes) == 3
    for node in nodes:
        conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
        assert conditions["Ready"] == "True", node["metadata"]["name"]


def test_environment_namespaces(kubectl: Kubectl) -> None:
    namespaces = kubectl.get("namespaces", "-l", "deploysim.io/simulator=true")["items"]

    assert sorted(ns["metadata"]["name"] for ns in namespaces) == sorted(ENVIRONMENTS)


def test_release_name_rule(kubectl: Kubectl, app: str) -> None:
    wrong_name = release(app, "1.0.0", image(app), name=f"{app}-9.9.9")

    result = kubectl.apply(wrong_name, namespace="dev", dry_run=True, check=False)

    assert result.returncode != 0
    assert "metadata.name must be <app>-<version>" in result.stderr


def test_release_spec_immutable(kubectl: Kubectl, app: str) -> None:
    original = release(app, "1.0.0", image(app, seed="a"))
    changed = release(app, "1.0.0", image(app, seed="b"))
    kubectl.apply(original, namespace="dev")
    kubectl.apply(original, namespace="dev")

    result = kubectl.apply(changed, namespace="dev", check=False)

    assert result.returncode != 0
    assert "Release spec is immutable; publish a new version" in result.stderr


def test_release_status_subresource(kubectl: Kubectl, app: str) -> None:
    name = f"{app}-1.0.0"
    kubectl.apply(release(app, "1.0.0", image(app)), namespace="dev")
    status = {"status": {"phase": "Succeeded", "attempts": 1}}

    kubectl.run(
        "patch",
        "releases.deploysim.io",
        name,
        "-n",
        "dev",
        "--subresource=status",
        "--type=merge",
        "-p",
        json.dumps(status),
    )

    stored = kubectl.get("releases.deploysim.io", name, "-n", "dev")
    assert stored["status"] == status["status"]
    header, row = kubectl.run("get", "releases.deploysim.io", name, "-n", "dev").stdout.splitlines()
    assert header.split() == ["NAME", "VERSION", "PHASE", "ATTEMPTS", "AGE"]
    assert row.split()[:4] == [name, "1.0.0", "Succeeded", "1"]


def test_stage_ready(kubectl: Kubectl, app: str) -> None:
    kubectl.apply(deployment(app, replicas=3), namespace="dev")

    wait_for(lambda: rollout_complete(kubectl.get("deployment", app, "-n", "dev")), timeout=10)


def test_stage_unhealthy(kubectl: Kubectl, app: str) -> None:
    deadline = 5
    kubectl.apply(deployment(app, fault="unhealthy", progress_deadline=deadline), namespace="dev")

    wait_for(
        lambda: progress_deadline_exceeded(kubectl.get("deployment", app, "-n", "dev")),
        timeout=deadline + 5,
    )

    [pod] = pods(kubectl, app)
    assert pod["status"]["phase"] == "Running"
    assert not ready(pod)
    [container] = pod["status"]["containerStatuses"]
    assert container["state"]["waiting"]["reason"] == "CrashLoopBackOff"


def test_stage_slow_start(kubectl: Kubectl, app: str) -> None:
    delay = stage_delay_seconds("pod-slow-start")
    started = time.monotonic()
    kubectl.apply(deployment(app, fault="slow-start"), namespace="dev")

    wait_for(lambda: pods(kubectl, app), timeout=5)
    time.sleep(1)
    assert not any(ready(p) for p in pods(kubectl, app))

    wait_for(lambda: all(ready(p) for p in pods(kubectl, app)), timeout=delay + 10)
    assert time.monotonic() - started >= delay - 1


def test_schemas_current(kubectl: Kubectl, tmp_path: Path) -> None:
    subprocess.run([str(REPO / "scripts" / "export-schemas.sh"), str(tmp_path)], check=True)

    for path in ("api/v1.json", "apis/apps/v1.json"):
        assert (tmp_path / path).read_bytes() == (REPO / "schemas" / path).read_bytes(), path


def test_bundle_configmap_immutable_on_server(kubectl: Kubectl, app: str) -> None:
    def configmap(value: str) -> Manifest:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"deploysim-{app}-1.0.0", "labels": {NAME_LABEL: app}},
            "immutable": True,
            "data": {"objects.json": value},
        }

    kubectl.apply(configmap("[]"), namespace="dev")

    changed = kubectl.apply(configmap('[{"kind": "Deployment"}]'), namespace="dev", check=False)

    assert changed.returncode != 0
    assert "field is immutable" in changed.stderr


def kube_config_digest() -> str | None:
    path = Path.home() / ".kube" / "config"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def makefile_version(name: str) -> str:
    match = re.search(rf"^{name}\s*:=\s*(\S+)$", (REPO / "Makefile").read_text(), re.MULTILINE)
    assert match, name
    return match.group(1)


def test_cluster_lifecycle(tmp_path: Path) -> None:
    """cluster-up on its own throwaway cluster: create, re-run, stopped, other fingerprint."""
    root = tmp_path / "root"
    root.mkdir()
    for name in ("bin", "kwok", "crds", "scripts"):
        (root / name).symlink_to(REPO / name)
    env = {
        **os.environ,
        "CLUSTER_NAME": f"deploysim-t{secrets.token_hex(3)}",
        "KWOK_VERSION": makefile_version("KWOK_VERSION"),
        "KWOK_KUBE_VERSION": makefile_version("KUBE_VERSION"),
    }
    kubeconfig = root / ".deploysim" / "kubeconfig"
    before = kube_config_digest()

    def script(name: str) -> subprocess.CompletedProcess[str]:
        command = [str(root / "scripts" / name)]
        return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)

    def versions() -> list[str]:
        local = Kubectl(kubeconfig)
        objects = local.get("namespaces", "-l", "deploysim.io/simulator=true")["items"]
        objects.append(local.get("crd", "releases.deploysim.io"))
        return [obj["metadata"]["resourceVersion"] for obj in objects]

    try:
        created = script("cluster-up.sh")
        assert created.returncode == 0, created.stderr
        assert "creating cluster" in created.stderr
        first = versions()

        rerun = script("cluster-up.sh")
        assert rerun.returncode == 0, rerun.stderr
        assert "creating cluster" not in rerun.stderr
        assert versions() == first

        subprocess.run(
            [str(REPO / "bin" / "kwokctl"), "stop", "cluster", "--name", env["CLUSTER_NAME"]],
            env=env,
            capture_output=True,
            check=True,
        )
        restarted = script("cluster-up.sh")
        assert restarted.returncode == 0, restarted.stderr
        assert "is stopped; recreating it empty" in restarted.stderr

        (root / ".deploysim" / "cluster.fingerprint").write_text("stale\n")
        refused = script("cluster-up.sh")
        assert refused.returncode == 1
        assert "created from other stages or versions" in refused.stderr
    finally:
        down = script("cluster-down.sh")
    assert down.returncode == 0, down.stderr
    assert not kubeconfig.exists()
    assert kube_config_digest() == before
