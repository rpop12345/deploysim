from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploysim.errors import ToolError, UnsafeTarget
from deploysim.kube import (
    AlreadyExists,
    Conflict,
    Forbidden,
    Invalid,
    Kubectl,
    KubeError,
    NotFound,
    Unreachable,
    classify,
)

REPO = Path(__file__).resolve().parents[2]

# stderr recorded from kubectl v1.34 against the KWOK cluster.
SAMPLES: dict[str, tuple[str, type[KubeError]]] = {
    "not-found": (
        'Error from server (NotFound): leases.coordination.k8s.io "nope" not found',
        NotFound,
    ),
    "already-exists": (
        'Error from server (AlreadyExists): error when creating "STDIN": '
        'leases.coordination.k8s.io "probe-lease" already exists',
        AlreadyExists,
    ),
    "conflict": (
        'Error from server (Conflict): error when replacing "STDIN": Operation cannot be '
        'fulfilled on leases.coordination.k8s.io "probe-lease": the object has been modified; '
        "please apply your changes to the latest version and try again",
        Conflict,
    ),
    "invalid-cel": (
        'The Release "x-9.9.9" is invalid: <nil>: Invalid value: "object": '
        "metadata.name must be <app>-<version>",
        Invalid,
    ),
    "invalid-immutable": (
        'The ConfigMap "probe-cm" is invalid: data: Forbidden: field is immutable when '
        "`immutable` is set",
        Invalid,
    ),
    "forbidden": (
        'Error from server (Forbidden): pods is forbidden: User "nobody" cannot list resource '
        '"pods" in API group "" in the namespace "dev"',
        Forbidden,
    ),
    "unreachable": (
        'E1024 12:18:43.867165   15118 memcache.go:265] "Unhandled Error" err="couldn\'t get '
        'current server API group list: Get \\"https://127.0.0.1:1/api?timeout=32s\\": dial tcp '
        '127.0.0.1:1: connect: connection refused"\n'
        "The connection to the server 127.0.0.1:1 was refused - did you specify the right host "
        "or port?",
        Unreachable,
    ),
    "unknown-resource": ('error: the server doesn\'t have a resource type "widgets"', KubeError),
}


def write_kubeconfig(path: Path, server: str, *, current_context: str = "kwok") -> Path:
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "kwok", "cluster": {"server": server}}],
        "users": [{"name": "kwok", "user": {}}],
        "contexts": [{"name": "kwok", "context": {"cluster": "kwok", "user": "kwok"}}],
        "current-context": current_context,
    }
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.fixture
def fake_kubectl(tmp_path: Path) -> Path:
    binary = tmp_path / "kubectl"
    binary.write_text("#!/bin/sh\n")
    return binary


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"subprocess.run called: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.mark.usefixtures("no_subprocess")
def test_guard_refuses_remote(tmp_path: Path, fake_kubectl: Path) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "config", "https://example.com:6443")

    with pytest.raises(UnsafeTarget, match=re.escape("refusing https://example.com:6443")):
        Kubectl(fake_kubectl, kubeconfig)


@pytest.mark.usefixtures("no_subprocess")
@pytest.mark.parametrize(
    "server", ["https://127.0.0.1:32764", "https://localhost:6443", "https://[::1]:6443"]
)
def test_guard_accepts_loopback(tmp_path: Path, fake_kubectl: Path, server: str) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "config", server)

    assert Kubectl(fake_kubectl, kubeconfig).server == server


@pytest.mark.usefixtures("no_subprocess")
def test_guard_missing_kubeconfig(tmp_path: Path, fake_kubectl: Path) -> None:
    with pytest.raises(UnsafeTarget, match="run make cluster-up"):
        Kubectl(fake_kubectl, tmp_path / "missing")


@pytest.mark.usefixtures("no_subprocess")
def test_guard_unknown_current_context(tmp_path: Path, fake_kubectl: Path) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "config", "https://127.0.0.1:1", current_context="x")

    with pytest.raises(UnsafeTarget, match="no server for the current context"):
        Kubectl(fake_kubectl, kubeconfig)


def test_missing_kubectl(tmp_path: Path) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "config", "https://127.0.0.1:1")

    with pytest.raises(ToolError, match="run make tools"):
        Kubectl(tmp_path / "kubectl", kubeconfig)


def test_run_pins_kubeconfig(
    tmp_path: Path, fake_kubectl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kubeconfig = write_kubeconfig(tmp_path / "config", "https://127.0.0.1:1")
    seen: dict[str, Any] = {}

    def fake_run(cmd: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("KUBECONFIG", "/somewhere/else")

    result = Kubectl(fake_kubectl, kubeconfig).run("get", "ns")

    assert result.cmd == (str(fake_kubectl), "--kubeconfig", str(kubeconfig), "get", "ns")
    assert seen["cmd"] == result.cmd
    assert seen["env"]["KUBECONFIG"] == str(kubeconfig)


@pytest.mark.parametrize("name", SAMPLES)
def test_error_classification(name: str) -> None:
    stderr, expected = SAMPLES[name]

    error = classify(("kubectl",), stderr)

    assert type(error) is expected
    assert str(error) == stderr.splitlines()[-1]


def test_only_kube_and_validate_spawn_processes() -> None:
    spawning = {
        path.name
        for path in (REPO / "deploysim").glob("*.py")
        if "import subprocess" in path.read_text()
    }

    assert spawning == {"kube.py", "validate.py"}
