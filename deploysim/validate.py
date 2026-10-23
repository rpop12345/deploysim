"""Pass-1 validation: YAML, bundle structure, kubectl-validate offline, then bundle policy."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deploysim.bundle import (
    RELEASE_TYPE,
    Bundle,
    BundleError,
    Violation,
    describe,
    parse_documents,
    policy_violations,
    structure_violations,
    type_of,
)
from deploysim.paths import Paths

_SKIPPED_RULES_NOTICE = "some validation rules were not checked"


class ToolError(Exception):
    """A required tool or file is missing or misbehaved; nothing was validated."""


@dataclass(frozen=True)
class ValidationResult:
    path: Path
    violations: tuple[Violation, ...]
    bundle: Bundle | None
    app: str | None
    cmd: tuple[str, ...] | None

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_file(path: Path, paths: Paths) -> ValidationResult:
    """Stops at the first failing layer: file, YAML, structure, schema, then policy."""
    try:
        text = path.read_text()
    except OSError as exc:
        return _rejected(path, Violation("file", "READ", None, str(exc)))
    try:
        docs = parse_documents(text)
    except BundleError as exc:
        return _rejected(path, Violation("yaml", "PARSE", None, str(exc)))

    app = _release_app(docs)
    structural = structure_violations(docs)
    if structural:
        return ValidationResult(path, tuple(structural), None, app, None)

    cmd, schema = run_kubectl_validate(path, docs, paths)
    if schema:
        return ValidationResult(path, schema, None, app, cmd)

    bundle = Bundle.from_documents(docs)
    return ValidationResult(path, tuple(policy_violations(bundle)), bundle, bundle.app, cmd)


def run_kubectl_validate(
    path: Path, docs: list[Any], paths: Paths
) -> tuple[tuple[str, ...], tuple[Violation, ...]]:
    """Runs with KUBECONFIG=/dev/null: it never contacts a cluster or runs a credential plugin."""
    for required in (paths.kubectl_validate, paths.schemas, paths.crds):
        if not required.exists():
            raise ToolError(f"{required} not found; run make tools, or set DEPLOYSIM_ROOT")
    cmd = (
        str(paths.kubectl_validate),
        str(path),
        "--local-schemas",
        str(paths.schemas),
        "--local-crds",
        str(paths.crds),
        "--output",
        "json",
    )
    env = {**os.environ, "KUBECONFIG": os.devnull}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False, timeout=60)
    try:
        report: dict[str, list[dict[str, Any]]] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"kubectl-validate exited {result.returncode}: {result.stderr.strip()}"
        ) from exc

    violations = []
    statuses = next(iter(report.values()), [])
    for index, status in enumerate(statuses):
        if status.get("status") != "Failure":
            continue
        document = describe(docs[index], index) if index < len(docs) else None
        details = status.get("details") or {}
        causes = [
            cause
            for cause in details.get("causes") or []
            if _SKIPPED_RULES_NOTICE not in cause.get("message", "")
        ]
        if not causes:
            message = status.get("message", "invalid")
            violations.append(Violation("kubectl-validate", "(object)", document, message))
        for cause in causes:
            field = cause.get("field")
            code = "(root)" if field in (None, "", "<nil>") else field
            message = cause.get("message", "")
            violations.append(Violation("kubectl-validate", code, document, message))
    return cmd, tuple(violations)


def _rejected(path: Path, violation: Violation) -> ValidationResult:
    return ValidationResult(path, (violation,), None, None, None)


def _release_app(docs: list[Any]) -> str | None:
    for doc in docs:
        if isinstance(doc, dict) and type_of(doc) == RELEASE_TYPE:
            spec = doc.get("spec")
            app = spec.get("app") if isinstance(spec, dict) else None
            return app if isinstance(app, str) else None
    return None
