"""kubectl access and manifest builders for end-to-end tests; independent of deploysim."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

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
