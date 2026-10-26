from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from deploysim.audit import AuditLog, Entry
from deploysim.cli import main

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text()
EXAMPLE = REPO / "examples" / "orders-api-1.0.0.yaml"


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DEPLOYSIM_ROOT": str(REPO)}
    return subprocess.run(
        [sys.executable, "-m", "deploysim", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=False,
    )


def readme_json(heading: str) -> dict[str, Any]:
    section = README.split(heading, 1)[1]
    loaded: dict[str, Any] = json.loads(section.split("```json", 1)[1].split("```", 1)[0])
    return loaded


def remote_kubeconfig(path: Path, server: str = "https://prod.example.com:6443") -> Path:
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "prod", "cluster": {"server": server}}],
        "users": [{"name": "admin", "user": {}}],
        "contexts": [{"name": "prod", "context": {"cluster": "prod", "user": "admin"}}],
        "current-context": "prod",
    }
    path.write_text(yaml.safe_dump(config))
    return path


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["deploy"],
        ["deploy", "bundle.yaml"],
        ["deploy", "bundle.yaml", "--env", "qa"],
        ["--bogus"],
        ["history", "--env", "nope"],
        ["recover", "--app", "a"],
    ],
    ids=["none", "no-file", "no-env", "bad-env", "bad-flag", "history-env", "recover-env"],
)
def test_usage_error_64(args: list[str]) -> None:
    result = cli(*args)

    assert result.returncode == 64
    assert "usage:" in result.stderr


def test_summary_schema(tmp_path: Path) -> None:
    fixture = REPO / "tests" / "fixtures" / "invalid" / "missing-readiness-probe.yaml"
    example = readme_json("### Deployment summary (stdout)")

    result = cli("deploy", str(fixture), "--env", "dev", "--audit-log", str(tmp_path / "a.jsonl"))

    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert list(summary) == [key for key in example if key != "error"]
    for key, value in summary.items():
        documented = example[key]
        if value is not None and documented is not None:
            assert type(value) is type(documented), key
    assert summary["rollback"].keys() == example["rollback"].keys()
    assert summary["recovery"].keys() == example["recovery"].keys()
    assert all(step.keys() >= {"seq", "stage", "action", "result"} for step in summary["steps"])


def test_history_verify_exit(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for n in range(3):
        log.append(Entry(stage="TEST", action="write", result="OK", detail={"n": n}))

    intact = cli("history", "--verify", "--audit-log", str(path))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[1].replace('"OK"', '"FAIL"'), lines[2]]) + "\n")
    altered = cli("history", "--verify", "--audit-log", str(path))

    assert intact.returncode == 0
    assert json.loads(intact.stdout) | {"audit_log": None} == {
        "audit_log": None,
        "records": 3,
        "verified": True,
        "problems": [],
    }
    assert altered.returncode == 1
    assert json.loads(altered.stdout)["verified"] is False
    assert "ALTERED line 2" in altered.stderr


def test_history_filters(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for app, env in [("a", "dev"), ("b", "dev"), ("a", "staging")]:
        log.append(Entry(stage="TEST", action="write", result="OK", app=app, environment=env))

    by_app = cli("history", "--app", "a", "--audit-log", str(path))
    by_both = cli("history", "--app", "a", "--env", "staging", "--audit-log", str(path))

    assert [json.loads(line)["seq"] for line in by_app.stdout.splitlines()] == [1, 3]
    assert [json.loads(line)["seq"] for line in by_both.stdout.splitlines()] == [3]


def test_unsafe_target_exit_5(tmp_path: Path) -> None:
    kubeconfig = str(remote_kubeconfig(tmp_path / "remote"))
    audit = str(tmp_path / "audit.jsonl")

    deploy = cli(
        "deploy", str(EXAMPLE), "--env", "dev", "--kubeconfig", kubeconfig, "--audit-log", audit
    )
    status = cli("status", "--app", "orders-api", "--env", "dev", "--kubeconfig", kubeconfig)

    assert deploy.returncode == 5
    summary = json.loads(deploy.stdout)
    assert summary["outcome"] == "ENVIRONMENT_ERROR"
    assert "refusing https://prod.example.com:6443" in summary["error"]
    assert status.returncode == 5
    assert "refusing https://prod.example.com:6443" in status.stderr


def test_unwritable_audit_log_exit_5(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)

    try:
        result = cli("validate", str(EXAMPLE), "--audit-log", str(locked / "audit.jsonl"))
    finally:
        locked.chmod(0o700)

    assert result.returncode == 5
    assert "audit log" in result.stderr
    assert "cannot write" in result.stderr
    assert "Traceback" not in result.stderr


def test_kubectl_timeout_exit_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_run = subprocess.run

    def kubectl_hangs(cmd: tuple[str, ...], **kwargs: Any) -> Any:
        if Path(cmd[0]).name == "kubectl":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", kubectl_hangs)
    monkeypatch.setenv("DEPLOYSIM_ROOT", str(REPO))
    kubeconfig = remote_kubeconfig(tmp_path / "kubeconfig", "https://127.0.0.1:1")
    audit = tmp_path / "audit.jsonl"
    args = ["--env", "dev", "--kubeconfig", str(kubeconfig), "--audit-log", str(audit)]

    code = main(["deploy", str(EXAMPLE), *args])

    assert code == 5
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "ENVIRONMENT_ERROR"
    assert summary["error"] == "kubectl timed out after 120 s"
    assert AuditLog(audit).records()[-1].result == "ERROR"
