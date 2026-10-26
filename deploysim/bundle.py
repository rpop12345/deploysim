"""Release bundles: YAML parsing, structural checks, bundle policy and the bundle digest."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

RELEASE_TYPE = ("deploysim.io/v1alpha1", "Release")
DEPLOYMENT_TYPE = ("apps/v1", "Deployment")
WORKLOAD_TYPES = frozenset({DEPLOYMENT_TYPE, ("v1", "Service"), ("v1", "ConfigMap")})
NAME_LABEL = "app.kubernetes.io/name"
VERSION_LABEL = "app.kubernetes.io/version"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_PROGRESS_DEADLINE_SECONDS = 600
DIGEST_PINNED = re.compile(r"@sha256:[a-f0-9]{64}$")

Manifest = dict[str, Any]


class BundleError(Exception):
    """The bundle file is not valid YAML."""


@dataclass(frozen=True)
class Violation:
    """source: file, yaml, bundle or kubectl-validate. code: a rule ID or a schema field path."""

    source: str
    code: str
    document: str | None
    message: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "code": self.code,
            "document": self.document,
            "message": self.message,
        }


def parse_documents(text: str) -> list[Any]:
    """Every YAML document in order; empty documents are None, matching kubectl-validate's count."""
    try:
        return list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise BundleError(str(exc)) from exc


def type_of(doc: Manifest) -> tuple[Any, Any]:
    return doc.get("apiVersion"), doc.get("kind")


def describe(doc: Any, index: int | None = None) -> str:
    """Kind/name, or "document N" (1-based) when the document has no kind and name."""
    if isinstance(doc, dict):
        metadata = doc.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if doc.get("kind") and name:
            return f"{doc['kind']}/{name}"
        if doc.get("kind") and index is None:
            return f"{doc['kind']}/<unnamed>"
    return f"document {index + 1}" if index is not None else "<unknown>"


def structure_violations(docs: list[Any]) -> list[Violation]:
    """Checks that need only apiVersion and kind; run before schema validation."""
    present = [(i, doc) for i, doc in enumerate(docs) if doc is not None]
    if not present:
        return [Violation("bundle", "EMPTY", None, "the file contains no YAML documents")]

    violations = []
    manifests = []
    for index, doc in present:
        where = describe(doc, index)
        if not isinstance(doc, dict):
            violations.append(Violation("bundle", "NOT_AN_OBJECT", where, "not a Kubernetes map"))
            continue
        manifests.append(doc)
        if type_of(doc) != RELEASE_TYPE and type_of(doc) not in WORKLOAD_TYPES:
            violations.append(
                Violation(
                    "bundle",
                    "KIND_ALLOWED",
                    where,
                    f"{doc.get('apiVersion')} {doc.get('kind')} is not allowed; a bundle holds one "
                    "Release plus apps/v1 Deployment, v1 Service and v1 ConfigMap objects",
                )
            )

    releases = sum(1 for doc in manifests if type_of(doc) == RELEASE_TYPE)
    if releases != 1:
        message = f"expected exactly one Release, found {releases}"
        violations.append(Violation("bundle", "ONE_RELEASE", None, message))
    if not any(type_of(doc) == DEPLOYMENT_TYPE for doc in manifests):
        violations.append(
            Violation("bundle", "HAS_DEPLOYMENT", None, "expected at least one apps/v1 Deployment")
        )
    return violations


def version_key(version: str) -> tuple[Any, ...]:
    """Sort key giving semantic-version precedence: 1.0.0-rc.1 < 1.0.0 < 1.0.1 < 1.1.0."""
    core, _, prerelease = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not prerelease:
        return (major, minor, patch, 1, ())
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in prerelease.split(".")
    )
    return (major, minor, patch, 0, identifiers)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Bundle:
    """A structurally valid bundle: one Release plus its workload objects."""

    release: Manifest
    objects: tuple[Manifest, ...]

    @classmethod
    def from_documents(cls, docs: list[Any]) -> Bundle:
        manifests = [doc for doc in docs if isinstance(doc, dict)]
        release = next(doc for doc in manifests if type_of(doc) == RELEASE_TYPE)
        return cls(release, tuple(doc for doc in manifests if doc is not release))

    @property
    def name(self) -> str:
        return str(self.release["metadata"]["name"])

    @property
    def app(self) -> str:
        return str(self.release["spec"]["app"])

    @property
    def version(self) -> str:
        return str(self.release["spec"]["version"])

    @property
    def image(self) -> str:
        return str(self.release["spec"]["image"])

    @property
    def timeout_seconds(self) -> int:
        health = self.release["spec"].get("healthCheck") or {}
        return int(health.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS))

    @property
    def spec(self) -> Manifest:
        """The Release spec with the CRD's defaults filled in, as the API server stores it."""
        health = {"timeoutSeconds": self.timeout_seconds}
        return {**self.release["spec"], "healthCheck": health}

    @property
    def deployments(self) -> tuple[Manifest, ...]:
        return tuple(obj for obj in self.objects if type_of(obj) == DEPLOYMENT_TYPE)

    @property
    def digest(self) -> str:
        """sha256 of the workload objects, ignoring document order, key order and YAML style."""
        ordered = sorted(canonical_json(obj) for obj in self.objects)
        return hashlib.sha256("\n".join(ordered).encode()).hexdigest()


def policy_violations(bundle: Bundle) -> list[Violation]:
    """Rules that span documents; run on schema-valid bundles only."""
    violations = []

    def violation(code: str, obj: Manifest, message: str) -> None:
        violations.append(Violation("bundle", code, describe(obj), message))

    for obj in bundle.objects:
        metadata = obj.get("metadata") or {}
        if "namespace" in metadata:
            violation("NO_NAMESPACE", obj, "metadata.namespace is set; deploysim assigns it")
        if (metadata.get("labels") or {}).get(NAME_LABEL) != bundle.app:
            violation("NAME_LABEL", obj, f"must be labelled {NAME_LABEL}: {bundle.app}")

    for deployment in bundle.deployments:
        spec = deployment["spec"]
        template = spec["template"]
        pod = template.get("spec") or {}
        containers = pod.get("containers") or []
        init_containers = pod.get("initContainers") or []

        labels = (template.get("metadata") or {}).get("labels") or {}
        if labels.get(VERSION_LABEL) != bundle.version:
            message = f"pod template must be labelled {VERSION_LABEL}: {bundle.version}"
            violation("VERSION_LABEL", deployment, message)

        for container in [*init_containers, *containers]:
            name, image = container.get("name"), container.get("image") or ""
            if not DIGEST_PINNED.search(image):
                message = f"container {name}: image {image!r} is not pinned by @sha256 digest"
                violation("IMAGE_DIGEST", deployment, message)

        app_container = next((c for c in containers if c.get("name") == bundle.app), None)
        if app_container is None:
            violation("IMAGE_MATCHES_RELEASE", deployment, f"no container named {bundle.app}")
        elif app_container.get("image") != bundle.image:
            image = app_container.get("image")
            message = f"container {bundle.app} runs {image}, Release names {bundle.image}"
            violation("IMAGE_MATCHES_RELEASE", deployment, message)

        for container in containers:
            if "readinessProbe" not in container:
                message = f"container {container.get('name')} has no readinessProbe"
                violation("READINESS_PROBE", deployment, message)

        deadline = spec.get("progressDeadlineSeconds")
        effective = DEFAULT_PROGRESS_DEADLINE_SECONDS if deadline is None else deadline
        if effective > bundle.timeout_seconds:
            shown = f"{effective} (default)" if deadline is None else str(effective)
            timeout = bundle.timeout_seconds
            message = f"progressDeadlineSeconds {shown} > healthCheck.timeoutSeconds {timeout}"
            violation("PROGRESS_DEADLINE", deployment, message)
    return violations
