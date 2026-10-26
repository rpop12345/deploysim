"""kubectl access and manifest builders for end-to-end tests; independent of deploysim."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml

REPO = Path(__file__).resolve().parents[2]
KUBECONFIG = REPO / ".deploysim" / "kubeconfig"
KUBECTL = REPO / "bin" / "kubectl"
ENVIRONMENTS = ("dev", "staging", "prod")
NAME_LABEL = "app.kubernetes.io/name"

T = TypeVar("T")
Manifest = dict[str, Any]


class KubectlError(Exception):
    def __init__(self, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        super().__init__(f"kubectl {' '.join(args)} exited {result.returncode}: {result.stderr}")
        self.result = result


class Kubectl:
    def __init__(self, kubeconfig: Path = KUBECONFIG) -> None:
        self.kubeconfig = kubeconfig

    def run(
        self, *args: str, stdin: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(KUBECTL), "--kubeconfig", str(self.kubeconfig), *args]
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise KubectlError(list(args), result)
        return result

    def get(self, *args: str) -> Manifest:
        loaded: Manifest = json.loads(self.run("get", *args, "-o", "json").stdout)
        return loaded

    def apply(
        self, *objects: Manifest, namespace: str, dry_run: bool = False, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        args = ["apply", "--server-side", "--field-manager=e2e", "-n", namespace, "-f", "-"]
        if dry_run:
            args.append("--dry-run=server")
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)})
        return self.run(*args, stdin=manifest, check=check)


def wait_for(probe: Callable[[], T | None], timeout: float, interval: float = 0.25) -> T:
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {timeout}s")
        time.sleep(interval)


def image(app: str, seed: str = "1") -> str:
    return f"registry.example.com/{app}@sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


def release(app: str, version: str, image_ref: str, name: str | None = None) -> Manifest:
    return {
        "apiVersion": "deploysim.io/v1alpha1",
        "kind": "Release",
        "metadata": {"name": name or f"{app}-{version}", "labels": {NAME_LABEL: app}},
        "spec": {"app": app, "version": version, "image": image_ref},
    }


def deployment(
    app: str,
    *,
    version: str = "1.0.0",
    replicas: int = 1,
    fault: str | None = None,
    progress_deadline: int = 10,
) -> Manifest:
    labels = {NAME_LABEL: app, "app.kubernetes.io/version": version}
    pod_metadata: Manifest = {"labels": labels}
    if fault:
        pod_metadata["annotations"] = {"deploysim.io/fault": fault}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app, "labels": {NAME_LABEL: app}},
        "spec": {
            "replicas": replicas,
            "progressDeadlineSeconds": progress_deadline,
            "selector": {"matchLabels": {NAME_LABEL: app}},
            "template": {
                "metadata": pod_metadata,
                "spec": {
                    "containers": [
                        {
                            "name": app,
                            "image": image(app),
                            "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}},
                        }
                    ]
                },
            },
        },
    }


def rollout_complete(dep: Manifest) -> bool:
    status = dep.get("status", {})
    replicas = dep["spec"]["replicas"]
    return bool(
        status.get("observedGeneration", 0) >= dep["metadata"]["generation"]
        and status.get("updatedReplicas") == replicas
        and status.get("readyReplicas") == replicas
        and status.get("availableReplicas") == replicas
    )


def progress_deadline_exceeded(dep: Manifest) -> bool:
    return any(
        c["type"] == "Progressing"
        and c["status"] == "False"
        and c.get("reason") == "ProgressDeadlineExceeded"
        for c in dep.get("status", {}).get("conditions", [])
    )


def bundle_documents(
    app: str,
    version: str = "1.0.0",
    *,
    image_seed: str | None = None,
    replicas: int = 2,
    fault: str | None = None,
    progress_deadline: int = 10,
    timeout: int = 20,
    readiness: bool = True,
    worker: bool = False,
) -> list[Manifest]:
    """A release bundle that passes validation unless readiness=False. worker adds a second
    Deployment, <app>-worker."""
    image_ref = image(app, image_seed or version)
    release_doc = release(app, version, image_ref)
    release_doc["spec"]["healthCheck"] = {"timeoutSeconds": timeout}
    del release_doc["metadata"]["labels"]
    workload = deployment(
        app, version=version, replicas=replicas, fault=fault, progress_deadline=progress_deadline
    )
    [container] = workload["spec"]["template"]["spec"]["containers"]
    container["image"] = image_ref
    if not readiness:
        del container["readinessProbe"]
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": app, "labels": {NAME_LABEL: app}},
        "spec": {"selector": {NAME_LABEL: app}, "ports": [{"port": 80, "targetPort": 8080}]},
    }
    documents = [release_doc, workload, service]
    if worker:
        extra = copy.deepcopy(workload)
        extra["metadata"]["name"] = f"{app}-worker"
        component = {"app.kubernetes.io/component": "worker"}
        extra["spec"]["selector"]["matchLabels"].update(component)
        extra["spec"]["template"]["metadata"]["labels"].update(component)
        documents.append(extra)
    return documents


def write_bundle(directory: Path, app: str, version: str = "1.0.0", **options: Any) -> Path:
    path = directory / f"{app}-{version}-{len(list(directory.glob('*.yaml')))}.yaml"
    path.write_text(yaml.safe_dump_all(bundle_documents(app, version, **options)))
    return path


@dataclass(frozen=True)
class CliRun:
    code: int
    summary: Manifest
    stderr: str


def start_deploysim(*args: str, audit_log: Path) -> subprocess.Popen[str]:
    """Starts the deploysim CLI from the repository root; finish() collects the result."""
    env = {**os.environ, "DEPLOYSIM_ROOT": str(REPO)}
    return subprocess.Popen(
        [sys.executable, "-m", "deploysim", *args, "--audit-log", str(audit_log)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=REPO,
    )


def finish(process: subprocess.Popen[str]) -> CliRun:
    stdout, stderr = process.communicate(timeout=300)
    try:
        summary: Manifest = json.loads(stdout)
    except json.JSONDecodeError:
        summary = {}
    return CliRun(process.returncode, summary, stderr)


def deploysim(*args: str, audit_log: Path) -> CliRun:
    """Runs the deploysim CLI and parses its JSON summary."""
    return finish(start_deploysim(*args, audit_log=audit_log))


CLEANUP_KINDS = "deployments,services,configmaps,releases.deploysim.io,leases"


def delete_app(kubectl: Kubectl, app: str) -> None:
    """Deletes everything carrying the app's name label in every environment."""
    for namespace in ENVIRONMENTS:
        kubectl.run(
            "delete",
            CLEANUP_KINDS,
            "-n",
            namespace,
            "-l",
            f"{NAME_LABEL}={app}",
            "--ignore-not-found",
            "--wait=false",
        )


def state_of(kubectl: Kubectl, app: str, namespace: str) -> Manifest:
    configmap = kubectl.get("configmap", f"deploysim-{app}", "-n", namespace)
    loaded: Manifest = json.loads(configmap["data"]["state.json"])
    return loaded


def labelled(kubectl: Kubectl, kinds: str, app: str, namespace: str) -> list[str]:
    """kind/name of every object of kinds carrying the app's name label."""
    items = kubectl.get(kinds, "-n", namespace, "-l", f"{NAME_LABEL}={app}")["items"]
    return [f"{item['kind']}/{item['metadata']['name']}" for item in items]
