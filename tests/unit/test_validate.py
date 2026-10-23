from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploysim.audit import AuditLog
from deploysim.bundle import Bundle, parse_documents
from deploysim.paths import Paths
from deploysim.validate import validate_file

REPO = Path(__file__).resolve().parents[2]
PATHS = Paths(REPO)
EXAMPLES = sorted((REPO / "examples").glob("*.yaml"))
FIXTURES = sorted((REPO / "tests" / "fixtures" / "invalid").glob("*.yaml"))
EXAMPLE = REPO / "examples" / "orders-api-1.1.0.yaml"


def expected_keys(path: Path) -> set[str]:
    prefix = "# expect: "
    lines = path.read_text().splitlines()
    return {line.removeprefix(prefix) for line in lines if line.startswith(prefix)}


def run_cli(*args: str, root: Path = REPO) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DEPLOYSIM_ROOT": str(root)}
    return subprocess.run(
        [sys.executable, "-m", "deploysim", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=False,
    )


def example_bundle() -> Bundle:
    return Bundle.from_documents(parse_documents(EXAMPLE.read_text()))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_valid(path: Path) -> None:
    result = validate_file(path, PATHS)

    assert result.violations == ()
    assert result.bundle is not None


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_invalid_fixture(path: Path) -> None:
    expected = expected_keys(path)
    assert expected, f"{path.name} has no '# expect:' header"

    result = validate_file(path, PATHS)

    assert {v.key for v in result.violations} == expected


def test_isolated_from_kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "credential-plugin-ran"
    hostile = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "c", "cluster": {"server": "https://127.0.0.1:1"}}],
        "users": [
            {
                "name": "u",
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": "/bin/sh",
                        "args": ["-c", f"touch {marker}; exit 1"],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
        "contexts": [{"name": "x", "context": {"cluster": "c", "user": "u"}}],
        "current-context": "x",
    }
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(yaml.safe_dump(hostile))
    (tmp_path / ".kube").mkdir()
    (tmp_path / ".kube" / "config").write_text(yaml.safe_dump(hostile))
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    monkeypatch.setenv("HOME", str(tmp_path))

    subprocess.run(
        [str(PATHS.kubectl_validate), str(EXAMPLE), "--local-schemas", str(PATHS.schemas)],
        capture_output=True,
        check=False,
    )
    assert marker.exists(), "control: unisolated kubectl-validate should run the plugin"
    marker.unlink()

    result = validate_file(EXAMPLE, PATHS)

    assert result.ok
    assert not marker.exists()


def test_bundle_digest_stable() -> None:
    original = example_bundle()

    reordered = Bundle(original.release, tuple(reversed(original.objects)))
    rekeyed = Bundle(original.release, tuple(_reverse_keys(obj) for obj in original.objects))
    restyled = yaml.safe_dump_all([original.release, *original.objects], indent=4)
    reformatted = Bundle.from_documents(list(yaml.safe_load_all(restyled)))
    changed_objects = [dict(obj) for obj in original.objects]
    deployment = next(obj for obj in changed_objects if obj["kind"] == "Deployment")
    deployment["spec"] = {**deployment["spec"], "replicas": 4}
    changed = Bundle(original.release, tuple(changed_objects))

    assert reordered.digest == original.digest
    assert rekeyed.digest == original.digest
    assert reformatted.digest == original.digest
    assert changed.digest != original.digest


def _reverse_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


def test_validation_time() -> None:
    validate_file(EXAMPLE, PATHS)
    started = time.monotonic()

    validate_file(EXAMPLE, PATHS)

    assert time.monotonic() - started < 2


def test_missing_file() -> None:
    result = validate_file(REPO / "examples" / "does-not-exist.yaml", PATHS)

    assert [v.key for v in result.violations] == ["file:READ"]


def test_cli_valid(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"

    result = run_cli("validate", str(EXAMPLE), "--audit-log", str(audit_path))

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["result"] == "VALID"
    assert output["release"] == "orders-api-1.1.0"
    assert output["bundle_digest"] == example_bundle().digest
    [record] = AuditLog(audit_path).records()
    assert (record.stage, record.action, record.result, record.app) == (
        "VALIDATE",
        "validate_bundle",
        "OK",
        "orders-api",
    )
    assert record.cmd is not None
    assert record.cmd[0].endswith("kubectl-validate")


def test_cli_rejected(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    fixture = REPO / "tests" / "fixtures" / "invalid" / "missing-readiness-probe.yaml"

    result = run_cli("validate", str(fixture), "--audit-log", str(audit_path))

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["result"] == "REJECTED"
    assert [(v["source"], v["code"]) for v in output["violations"]] == [
        ("bundle", "READINESS_PROBE")
    ]
    assert "REJECTED Deployment/orders-api: bundle:READINESS_PROBE" in result.stderr
    [record] = AuditLog(audit_path).records()
    assert record.result == "REJECTED"
    assert record.detail["violations"] == ["bundle:READINESS_PROBE"]


def test_tool_missing(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"

    result = run_cli("validate", str(EXAMPLE), "--audit-log", str(audit_path), root=tmp_path)

    assert result.returncode == 5
    assert json.loads(result.stdout)["result"] == "ENVIRONMENT_ERROR"
    assert "run make tools" in result.stderr
    [record] = AuditLog(audit_path).records()
    assert record.result == "ERROR"
