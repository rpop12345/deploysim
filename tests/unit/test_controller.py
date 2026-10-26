from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from deploysim.bundle import Bundle, parse_documents
from deploysim.controller import (
    DeployRequest,
    bundle_configmap,
    deployment_id,
    release_manifest,
    workload_manifests,
)
from deploysim.outcomes import ExitCode, Outcome

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "orders-api-1.1.0.yaml"


def example() -> Bundle:
    return Bundle.from_documents(parse_documents(EXAMPLE.read_text()))


def test_deployment_id_stable() -> None:
    bundle = example()
    without_defaults = replace(
        bundle,
        release={
            **bundle.release,
            "spec": {k: v for k, v in bundle.release["spec"].items() if k != "healthCheck"},
        },
    )
    other_objects = replace(bundle, objects=bundle.objects[:-1])

    assert deployment_id(bundle, "dev") == deployment_id(example(), "dev")
    assert deployment_id(bundle, "dev").startswith("dep-")
    assert len(deployment_id(bundle, "dev")) == len("dep-") + 12
    assert deployment_id(without_defaults, "dev") == deployment_id(bundle, "dev")
    assert deployment_id(bundle, "staging") != deployment_id(bundle, "dev")
    assert deployment_id(other_objects, "dev") != deployment_id(bundle, "dev")


def test_in_cluster_manifests_carry_labels_and_digest() -> None:
    bundle = example()
    labels = {"app.kubernetes.io/name": "orders-api", "app.kubernetes.io/managed-by": "deploysim"}

    release = release_manifest(bundle)
    stored = bundle_configmap(bundle)
    workloads = workload_manifests(bundle.objects, bundle.app)

    assert release["metadata"]["annotations"]["deploysim.io/bundle-digest"] == bundle.digest
    assert release["spec"]["healthCheck"] == {"timeoutSeconds": 60}
    assert stored["immutable"] is True
    assert stored["metadata"]["name"] == "deploysim-orders-api-1.1.0"
    for manifest in [release, stored, *workloads]:
        assert labels.items() <= manifest["metadata"]["labels"].items()
    assert "managed-by" not in str(bundle.objects)


def test_outcome_exit_codes() -> None:
    assert Outcome.SUCCEEDED.exit_code == ExitCode.SUCCESS
    assert Outcome.NO_OP.exit_code == ExitCode.SUCCESS
    assert Outcome.RECOVERED.exit_code == ExitCode.SUCCESS
    assert Outcome.REJECTED.exit_code == ExitCode.REJECTED
    assert Outcome.ROLLED_BACK.exit_code == ExitCode.ROLLED_BACK
    assert Outcome.ROLLBACK_FAILED.exit_code == ExitCode.ROLLBACK_FAILED
    assert Outcome.LOCKED.exit_code == ExitCode.LOCKED
    assert Outcome.ENVIRONMENT_ERROR.exit_code == ExitCode.ENVIRONMENT_ERROR


def test_fault_injected_only_into_pod_templates() -> None:
    bundle = example()

    faulty = workload_manifests(bundle.objects, bundle.app, "unhealthy")

    [deployment] = [m for m in faulty if m["kind"] == "Deployment"]
    template = deployment["spec"]["template"]["metadata"]
    assert template["annotations"]["deploysim.io/fault"] == "unhealthy"
    assert all("deploysim.io/fault" not in str(m) for m in faulty if m["kind"] != "Deployment")
    assert "deploysim.io/fault" not in str(bundle.objects)
    assert "deploysim.io/fault" not in str(bundle_configmap(bundle))


def test_fault_targets() -> None:
    plain = DeployRequest(EXAMPLE, "dev")
    target = DeployRequest(EXAMPLE, "dev", inject_fault="unhealthy")
    both = DeployRequest(EXAMPLE, "dev", inject_fault="unhealthy-rollback")

    assert (plain.target_fault, plain.rollback_fault) == (None, None)
    assert (target.target_fault, target.rollback_fault) == ("unhealthy", None)
    assert (both.target_fault, both.rollback_fault) == ("unhealthy", "unhealthy")


def test_crash_faults() -> None:
    during_rollback = DeployRequest(EXAMPLE, "dev", inject_fault="crash-during-rollback")
    after_deploy = DeployRequest(EXAMPLE, "dev", inject_fault="crash-after-deploy")

    assert during_rollback.target_fault == "unhealthy"
    assert during_rollback.rollback_fault is None
    assert (after_deploy.target_fault, after_deploy.rollback_fault) == (None, None)
    assert int(ExitCode.SIMULATED_CRASH) == 70
