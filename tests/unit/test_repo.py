"""Repository-level claims the README makes: pinning, dependencies, kubeconfig isolation."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLATFORMS = ("darwin_amd64", "darwin_arm64", "linux_amd64", "linux_arm64")
TOOLS = ("KWOKCTL", "KWOK", "KUBECTL", "KUBECTL_VALIDATE")


def test_actions_pinned_by_commit_sha() -> None:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    uses = [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]

    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action


def test_only_runtime_dependency_is_pyyaml() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]

    assert [re.split(r"[<>=!~ ]", dep)[0] for dep in project["dependencies"]] == ["PyYAML"]


def test_every_tool_pinned_for_every_platform() -> None:
    makefile = (REPO / "Makefile").read_text()

    for tool in TOOLS:
        for platform in PLATFORMS:
            pattern = rf"^{tool}_SHA256_{platform}\s*:=\s*[0-9a-f]{{64}}$"
            assert re.search(pattern, makefile, re.MULTILINE), f"{tool} {platform}"


def test_scripts_never_use_the_users_kubeconfig() -> None:
    for script in (REPO / "scripts").glob("*.sh"):
        text = script.read_text().replace("\\\n", " ")
        assert "~/.kube" not in text, script.name
        assert "$HOME/.kube" not in text, script.name
        for line in text.splitlines():
            if re.search(r"bin/kwokctl (create|delete) cluster", line):
                assert "--kubeconfig" in line, f"{script.name}: {line.strip()}"
            if "bin/kubectl " in line:
                assert "--kubeconfig" in line, f"{script.name}: {line.strip()}"
