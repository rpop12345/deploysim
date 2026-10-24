"""The only module that runs kubectl: safety guard, error classification, namespaced calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from deploysim.errors import EnvironmentProblem, ToolError, UnsafeTarget
from deploysim.paths import Paths

SIMULATOR_LABEL = "deploysim.io/simulator"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
FIELD_MANAGER = "deploysim"

Manifest = dict[str, Any]


class KubeError(Exception):
    """kubectl exited non-zero. The message is the last line of its stderr."""

    def __init__(self, cmd: tuple[str, ...], stderr: str) -> None:
        lines = [line for line in stderr.strip().splitlines() if line.strip()]
        super().__init__(lines[-1] if lines else f"kubectl failed: {' '.join(cmd)}")
        self.cmd = cmd
        self.stderr = stderr


class NotFound(KubeError):
    pass


class AlreadyExists(KubeError):
    pass


class Conflict(KubeError):
    """The object changed since it was read: a compare-and-swap write lost."""


class Invalid(KubeError):
    pass


class Forbidden(KubeError):
    pass


class Unreachable(KubeError, EnvironmentProblem):
    pass


_CLASSIFIERS: tuple[tuple[re.Pattern[str], type[KubeError]], ...] = (
    (re.compile(r"\(NotFound\)"), NotFound),
    (re.compile(r"\(AlreadyExists\)"), AlreadyExists),
    (re.compile(r"\(Conflict\)"), Conflict),
    (re.compile(r" is invalid: |\(Invalid\)"), Invalid),
    (re.compile(r"\(Forbidden\)"), Forbidden),
    (
        re.compile(
            r"connection to the server .* was refused|Unable to connect to the server"
            r"|connect: connection refused|i/o timeout|no such host"
        ),
        Unreachable,
    ),
)


def classify(cmd: tuple[str, ...], stderr: str) -> KubeError:
    """Checks Invalid before Forbidden: immutable-field errors say "is invalid: ... Forbidden"."""
    for pattern, error in _CLASSIFIERS:
        if pattern.search(stderr):
            return error(cmd, stderr)
    return KubeError(cmd, stderr)


def current_server(kubeconfig: Path) -> str:
    try:
        config = yaml.safe_load(kubeconfig.read_text())
    except FileNotFoundError as exc:
        raise UnsafeTarget(f"{kubeconfig} not found; run make cluster-up") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise UnsafeTarget(f"{kubeconfig}: {exc}") from exc
    try:
        context_name = config["current-context"]
        context = next(c["context"] for c in config["contexts"] if c["name"] == context_name)
        cluster = next(c["cluster"] for c in config["clusters"] if c["name"] == context["cluster"])
        return str(cluster["server"])
    except (KeyError, TypeError, StopIteration) as exc:
        raise UnsafeTarget(f"{kubeconfig}: no server for the current context") from exc


def check_loopback(kubeconfig: Path) -> str:
    server = current_server(kubeconfig)
    if urlparse(server).hostname not in LOOPBACK_HOSTS:
        raise UnsafeTarget(f"refusing {server}: deploysim only talks to a cluster on loopback")
    return server


def managed_labels(app: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": app, "app.kubernetes.io/managed-by": "deploysim"}


@dataclass(frozen=True)
class Result:
    cmd: tuple[str, ...]
    stdout: str

    def json(self) -> Manifest:
        loaded: Manifest = json.loads(self.stdout)
        return loaded

    def items(self) -> list[Manifest]:
        """The objects printed: kubectl prints one object bare and several as a List."""
        loaded = self.json()
        return list(loaded["items"]) if loaded.get("kind") == "List" else [loaded]


@dataclass(frozen=True)
class Drift:
    """Objects an apply would create or change. Objects still changing after every attempt
    are counted as changed."""

    cmds: tuple[tuple[str, ...], ...]
    changed: tuple[str, ...]


DRIFT_ATTEMPTS = 5


def _comparable(obj: Manifest) -> Manifest:
    metadata = obj.get("metadata") or {}
    trimmed = {key: value for key, value in obj.items() if key != "status"}
    trimmed["metadata"] = {k: v for k, v in metadata.items() if k != "managedFields"}
    return trimmed


def _key(obj: Manifest) -> str:
    return f"{obj['kind']}/{obj['metadata']['name']}"


class Kubectl:
    """kubectl bound to one kubeconfig, which must point at a loopback API server."""

    def __init__(self, binary: Path, kubeconfig: Path) -> None:
        if not binary.exists():
            raise ToolError(f"{binary} not found; run make tools")
        self.server = check_loopback(kubeconfig)
        self.binary = binary
        self.kubeconfig = kubeconfig

    @classmethod
    def connect(cls, paths: Paths, kubeconfig: Path | None = None) -> Kubectl:
        return cls(paths.kubectl, kubeconfig or paths.kubeconfig)

    def run(self, *args: str, stdin: object = None) -> Result:
        """stdin is sent as JSON. Raises a classified KubeError on a non-zero exit."""
        cmd = (str(self.binary), "--kubeconfig", str(self.kubeconfig), *args)
        env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}
        completed = subprocess.run(
            cmd,
            input=None if stdin is None else json.dumps(stdin),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise classify(cmd, completed.stderr)
        return Result(cmd, completed.stdout)

    def environment(self, name: str) -> Environment:
        """The namespace must exist and carry deploysim.io/simulator=true."""
        try:
            namespace = self.run("get", "namespace", name, "-o", "json").json()
        except NotFound as exc:
            raise UnsafeTarget(f"namespace {name} does not exist") from exc
        labels = namespace["metadata"].get("labels") or {}
        if labels.get(SIMULATOR_LABEL) != "true":
            raise UnsafeTarget(f"namespace {name} is not labelled {SIMULATOR_LABEL}=true")
        return Environment(self, name)


@dataclass(frozen=True)
class Environment:
    """Operations in one guarded namespace. Every method returns the exact command it ran."""

    kubectl: Kubectl
    name: str

    def get(self, kind: str, name: str) -> Result:
        return self.kubectl.run("get", kind, name, "-n", self.name, "-o", "json")

    def get_labelled(self, kind: str, selector: str) -> Result:
        return self.kubectl.run("get", kind, "-n", self.name, "-l", selector, "-o", "json")

    def create(self, manifest: Manifest) -> Result:
        return self.kubectl.run("create", "-n", self.name, "-f", "-", "-o", "json", stdin=manifest)

    def replace(self, manifest: Manifest) -> Result:
        """Compare-and-swap when manifest carries metadata.resourceVersion: Conflict if stale."""
        return self.kubectl.run("replace", "-n", self.name, "-f", "-", "-o", "json", stdin=manifest)

    def apply(self, manifests: list[Manifest], *, dry_run: bool = False) -> Result:
        args = [*self._apply_args("apply"), "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return self.kubectl.run(*args, stdin=_as_list(manifests))

    def drift(self, manifests: list[Manifest]) -> Drift:
        """Compares each live object with a server-side dry-run apply computed from the same
        resourceVersion, retrying objects a controller updated between the two reads."""
        cmds: list[tuple[str, ...]] = []
        changed: set[str] = set()
        pending = list(manifests)
        for _ in range(DRIFT_ATTEMPTS):
            live: dict[str, Manifest] = {}
            for manifest in pending:
                try:
                    result = self.get(manifest["kind"], manifest["metadata"]["name"])
                except NotFound:
                    continue
                cmds.append(result.cmd)
                live[_key(manifest)] = result.json()
            dry_run = self.apply(pending, dry_run=True)
            cmds.append(dry_run.cmd)
            moved = []
            for after in dry_run.items():
                before = live.get(_key(after))
                if before is None:
                    changed.add(_key(after))
                elif before["metadata"]["resourceVersion"] != after["metadata"]["resourceVersion"]:
                    moved.append(_key(after))
                elif _comparable(before) != _comparable(after):
                    changed.add(_key(after))
            pending = [m for m in pending if _key(m) in moved]
            if not pending:
                break
        changed.update(_key(m) for m in pending)
        ordered = tuple(_key(m) for m in manifests if _key(m) in changed)
        return Drift(tuple(cmds), ordered)

    def delete(self, kind: str, name: str, *, wait: bool = False) -> Result:
        flags = ["--ignore-not-found", f"--wait={str(wait).lower()}"]
        return self.kubectl.run("delete", kind, name, "-n", self.name, *flags)

    def patch_status(self, kind: str, name: str, patch: Manifest) -> Result:
        args = ["patch", kind, name, "-n", self.name, "--subresource=status", "--type=merge"]
        return self.kubectl.run(*args, "-p", json.dumps(patch), "-o", "json")

    def _apply_args(self, verb: str) -> list[str]:
        manager = f"--field-manager={FIELD_MANAGER}"
        return [verb, "--server-side", manager, "--force-conflicts", "-n", self.name, "-f", "-"]


def _as_list(manifests: list[Manifest]) -> Manifest:
    return {"apiVersion": "v1", "kind": "List", "items": manifests}
