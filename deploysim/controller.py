"""deploy: VALIDATE, LOCK, cluster validation, PREPARE, DEPLOY, VERIFY, COMPLETE."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from deploysim.audit import AuditLog, Entry
from deploysim.bundle import Bundle, Violation, canonical_json
from deploysim.clock import iso_millis, utcnow
from deploysim.errors import EnvironmentProblem
from deploysim.health import Verification, verify
from deploysim.kube import (
    AlreadyExists,
    Conflict,
    Environment,
    Invalid,
    Kubectl,
    KubeError,
    Manifest,
    NotFound,
    managed_labels,
)
from deploysim.lease import HeldLease, Locked, acquire, default_holder, release, renew
from deploysim.outcomes import Outcome
from deploysim.paths import Paths
from deploysim.state import InFlight, LastOutcome, ReleaseRef, StateStore, Status, Stored
from deploysim.validate import validate_file

ENVIRONMENTS = ("dev", "staging", "prod")
RELEASES = "releases.deploysim.io"
DIGEST_ANNOTATION = "deploysim.io/bundle-digest"
BUNDLE_KEY = "objects.json"

Detail = dict[str, Any]


class LeaseLost(EnvironmentProblem):
    """Another run took the Lease over; this run stopped writing."""


def deployment_id(bundle: Bundle, environment: str) -> str:
    """dep- plus 12 hex of sha256(app | environment | Release spec | bundle digest)."""
    material = "|".join([bundle.app, environment, canonical_json(bundle.spec), bundle.digest])
    return "dep-" + hashlib.sha256(material.encode()).hexdigest()[:12]


def bundle_configmap_name(bundle: Bundle) -> str:
    return f"deploysim-{bundle.app}-{bundle.version}"


def release_manifest(bundle: Bundle) -> Manifest:
    return {
        "apiVersion": "deploysim.io/v1alpha1",
        "kind": "Release",
        "metadata": {
            "name": bundle.name,
            "labels": managed_labels(bundle.app),
            "annotations": {DIGEST_ANNOTATION: bundle.digest},
        },
        "spec": bundle.spec,
    }


def bundle_configmap(bundle: Bundle) -> Manifest:
    """The workload objects as applied, kept for rollback. Immutable."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": bundle_configmap_name(bundle),
            "labels": managed_labels(bundle.app),
            "annotations": {DIGEST_ANNOTATION: bundle.digest},
        },
        "immutable": True,
        "data": {BUNDLE_KEY: json.dumps(list(bundle.objects), indent=2, sort_keys=True)},
    }


def workload_manifests(bundle: Bundle) -> list[Manifest]:
    """Bundle objects with deploysim's labels added."""
    manifests = []
    for obj in bundle.objects:
        manifest = copy.deepcopy(obj)
        metadata = manifest.setdefault("metadata", {})
        metadata["labels"] = {**(metadata.get("labels") or {}), **managed_labels(bundle.app)}
        manifests.append(manifest)
    return manifests


def describe_object(obj: Manifest) -> str:
    return f"{obj['kind']}/{obj['metadata']['name']}"


def bundle_digest_of(obj: Manifest) -> str | None:
    annotations = (obj.get("metadata") or {}).get("annotations") or {}
    digest = annotations.get(DIGEST_ANNOTATION)
    return str(digest) if digest is not None else None


@dataclass(frozen=True)
class DeployRequest:
    file: Path
    environment: str
    allow_degraded: bool = False
    lease_seconds: int = 30
    holder: str = field(default_factory=default_holder)


@dataclass(frozen=True)
class DeployReport:
    outcome: Outcome
    summary: dict[str, Any]


@dataclass(frozen=True)
class Ending:
    outcome: Outcome
    violations: tuple[Violation, ...] = ()
    error: str | None = None


class Run:
    """Audit records for one invocation: written to the log, echoed to stderr, kept as steps."""

    def __init__(self, audit: AuditLog, progress: IO[str], environment: str, started: datetime):
        self.audit = audit
        self.progress = progress
        self.environment = environment
        self.started = started
        self.app: str | None = None
        self.deployment_id: str | None = None
        self.attempt: int | None = None
        self.stage = "VALIDATE"
        self.from_release: str | None = None
        self.to_release: str | None = None
        self.final_release: str | None = None
        self.environment_status: Status | None = None
        self.steps: list[Detail] = []

    def record(
        self,
        stage: str,
        action: str,
        result: str,
        *,
        cmd: tuple[str, ...] | None = None,
        detail: Detail | None = None,
    ) -> None:
        self.stage = stage
        entry = Entry(
            stage=stage,
            action=action,
            result=result,
            deployment_id=self.deployment_id,
            attempt=self.attempt,
            app=self.app,
            environment=self.environment,
            cmd=list(cmd) if cmd else None,
            detail=detail or {},
        )
        record = self.audit.append(entry)
        step: Detail = {"seq": record.seq, "stage": stage, "action": action, "result": result}
        if detail:
            step["detail"] = detail
        self.steps.append(step)
        print(f"deploysim: {stage:<9} {action:<16} {result}", file=self.progress)

    def summary(self, ending: Ending, finished: datetime) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "deployment_id": self.deployment_id,
            "attempt": self.attempt,
            "app": self.app,
            "environment": self.environment,
            "from": self.from_release,
            "to": self.to_release,
            "outcome": ending.outcome.value,
            "rollback": {"performed": False, "to": None, "result": None},
            "final": self.final_release,
            "environment_status": self.environment_status,
            "started_at": iso_millis(self.started),
            "finished_at": iso_millis(finished),
            "violations": [violation.to_dict() for violation in ending.violations],
            "steps": self.steps,
        }
        if ending.error:
            summary["error"] = ending.error
        return summary


class Controller:
    def __init__(
        self,
        paths: Paths,
        audit: AuditLog,
        *,
        kubeconfig: Path | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 0.5,
        progress: IO[str] = sys.stderr,
    ) -> None:
        self.paths = paths
        self.audit = audit
        self.kubeconfig = kubeconfig
        self.clock = clock
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.progress = progress

    def deploy(self, request: DeployRequest) -> DeployReport:
        return Deploy(self, request).execute()


class LeaseKeeper:
    """Holds this run's Lease: renewed on every VERIFY poll, released at the end."""

    def __init__(self, lease: HeldLease, clock: Callable[[], datetime]) -> None:
        self.lease: HeldLease | None = lease
        self.clock = clock

    def renew(self) -> None:
        if self.lease is None:
            raise LeaseLost("the lease is no longer held")
        try:
            self.lease = renew(self.lease, self.clock())
        except Conflict as exc:
            self.lease = None
            raise LeaseLost("another run took over the lease; stopped") from exc

    def release(self, run: Run) -> None:
        if self.lease is None:
            return
        lease, self.lease = self.lease, None
        try:
            cmd = release(lease, self.clock())
        except KubeError as exc:
            run.record("LOCK", "release_lease", "ERROR", cmd=exc.cmd, detail={"error": str(exc)})
            return
        run.record("LOCK", "release_lease", "OK", cmd=cmd)


class Deploy:
    """One deploysim deploy invocation, up to holding the Lease."""

    def __init__(self, controller: Controller, request: DeployRequest) -> None:
        self.ctl = controller
        self.request = request
        started = controller.clock()
        self.run = Run(controller.audit, controller.progress, request.environment, started)
        self.keeper: LeaseKeeper | None = None

    def execute(self) -> DeployReport:
        try:
            ending = self._execute()
        except (EnvironmentProblem, KubeError) as exc:
            cmd = exc.cmd if isinstance(exc, KubeError) else None
            self.run.record(self.run.stage, "error", "ERROR", cmd=cmd, detail={"error": str(exc)})
            ending = Ending(Outcome.ENVIRONMENT_ERROR, error=str(exc))
        if self.keeper:
            self.keeper.release(self.run)
        return DeployReport(ending.outcome, self.run.summary(ending, self.ctl.clock()))

    def _execute(self) -> Ending:
        bundle = self._validate_offline()
        if isinstance(bundle, Ending):
            return bundle
        kubectl = Kubectl.connect(self.ctl.paths, self.ctl.kubeconfig)
        env = kubectl.environment(self.request.environment)
        lease = self._lock(env, bundle)
        if isinstance(lease, Ending):
            return lease
        self.keeper = LeaseKeeper(lease, self.ctl.clock)

        session = Session.open(self.ctl, self.request, self.run, kubectl, env, bundle, self.keeper)
        rejected = session.validate_cluster()
        if rejected:
            return rejected
        session.prepare()
        session.deploy()
        verification = session.verify()
        if not verification.ok:
            error = f"health check failed: {verification.reason}"
            return Ending(Outcome.VERIFY_FAILED, error=error)
        session.complete()
        return Ending(Outcome.SUCCEEDED)

    def _validate_offline(self) -> Bundle | Ending:
        request, run = self.request, self.run
        validation = validate_file(request.file, self.ctl.paths)
        run.app = validation.app
        detail: Detail = {"file": str(request.file)}
        if validation.bundle:
            detail["bundle_digest"] = validation.bundle.digest
        detail["violations"] = [violation.key for violation in validation.violations]
        if validation.ok and validation.bundle:
            run.deployment_id = deployment_id(validation.bundle, request.environment)
            run.to_release = validation.bundle.name
        result = "OK" if validation.ok else "REJECTED"
        run.record("VALIDATE", "validate_bundle", result, cmd=validation.cmd, detail=detail)
        if not validation.ok or validation.bundle is None:
            return Ending(Outcome.REJECTED, validation.violations)

        if request.environment not in ENVIRONMENTS:
            allowed = ", ".join(ENVIRONMENTS)
            violation = Violation("environment", "UNKNOWN", None, f"must be one of {allowed}")
            detail = {"violations": [violation.key]}
            run.record("VALIDATE", "check_environment", "REJECTED", detail=detail)
            return Ending(Outcome.REJECTED, (violation,))
        return validation.bundle

    def _lock(self, env: Environment, bundle: Bundle) -> HeldLease | Ending:
        request = self.request
        now = self.ctl.clock()
        try:
            lease = acquire(env, bundle.app, request.holder, request.lease_seconds, now)
        except Locked as exc:
            expires = iso_millis(exc.expires_at) if exc.expires_at else None
            detail: Detail = {"holder": exc.holder, "expires_at": expires}
            self.run.record("LOCK", "acquire_lease", "LOCKED", detail=detail)
            return Ending(Outcome.LOCKED, error=f"lease {exc}")
        detail = {"holder": lease.holder, "took_over_from": lease.took_over_from}
        self.run.record("LOCK", "acquire_lease", "OK", cmd=lease.cmd, detail=detail)
        return lease


@dataclass
class Session:
    """The stages that run while this run holds the Lease."""

    ctl: Controller
    request: DeployRequest
    run: Run
    kubectl: Kubectl
    env: Environment
    bundle: Bundle
    keeper: LeaseKeeper
    store: StateStore
    stored: Stored

    @classmethod
    def open(
        cls,
        ctl: Controller,
        request: DeployRequest,
        run: Run,
        kubectl: Kubectl,
        env: Environment,
        bundle: Bundle,
        keeper: LeaseKeeper,
    ) -> Session:
        """Loads the environment's state and records it."""
        store = StateStore(env, bundle.app)
        stored = store.load()
        state = stored.state
        run.attempt = state.attempts.get(run.deployment_id or "", 0) + 1
        run.from_release = state.current.release if state.current else None
        run.final_release = run.from_release
        run.environment_status = state.status
        in_flight = state.in_flight.deployment_id if state.in_flight else None
        detail = {"status": state.status, "current": run.from_release, "in_flight": in_flight}
        run.record("VALIDATE", "load_state", "OK", cmd=stored.cmd, detail=detail)
        return cls(ctl, request, run, kubectl, env, bundle, keeper, store, stored)

    def _save(self, stage: str, action: str, detail: Detail, **changes: Any) -> None:
        state = replace(self.stored.state, **changes)
        self.stored = self.store.save(self.stored, state, self.ctl.clock())
        self.run.record(stage, action, "OK", cmd=self.stored.cmd, detail=detail)

    # VALIDATE (cluster) ---------------------------------------------------------------------

    def validate_cluster(self) -> Ending | None:
        violations = [*self._degraded_gate(), *self._promotion_gate(), *self._immutability()]
        if violations:
            return Ending(Outcome.REJECTED, tuple(violations))
        manifests = [
            release_manifest(self.bundle),
            bundle_configmap(self.bundle),
            *workload_manifests(self.bundle),
        ]
        try:
            result = self.env.apply(manifests, dry_run=True)
        except Invalid as exc:
            violation = Violation("server", "DRY_RUN", None, str(exc))
            detail = {"violations": [violation.key], "error": str(exc)}
            self.run.record("VALIDATE", "server_dry_run", "REJECTED", cmd=exc.cmd, detail=detail)
            return Ending(Outcome.REJECTED, (violation,))
        self.run.record("VALIDATE", "server_dry_run", "OK", cmd=result.cmd)
        return None

    def _degraded_gate(self) -> list[Violation]:
        if self.stored.state.status != Status.DEGRADED or self.request.allow_degraded:
            return []
        message = "the last rollback failed; pass --allow-degraded to deploy anyway"
        violation = Violation("environment", "DEGRADED", None, message)
        detail = {"violations": [violation.key]}
        self.run.record("VALIDATE", "degraded_gate", "REJECTED", detail=detail)
        return [violation]

    def _promotion_gate(self) -> list[Violation]:
        """staging and prod need this Release and bundle to have Succeeded one step earlier."""
        bundle = self.bundle
        index = ENVIRONMENTS.index(self.request.environment)
        if index == 0:
            return []
        previous = ENVIRONMENTS[index - 1]
        earlier: Manifest | None
        try:
            result = self.kubectl.environment(previous).get(RELEASES, bundle.name)
            earlier, cmd = result.json(), result.cmd
        except NotFound as exc:
            earlier, cmd = None, exc.cmd

        phase = ((earlier or {}).get("status") or {}).get("phase")
        problem = None
        if earlier is None:
            problem = f"{bundle.name} has never been deployed to {previous}"
        elif earlier["spec"] != bundle.spec or bundle_digest_of(earlier) != bundle.digest:
            problem = f"{bundle.name} in {previous} has a different spec or bundle"
        elif phase != "Succeeded":
            problem = f"{bundle.name} has not succeeded in {previous} (phase {phase})"

        violations = []
        if problem:
            violations.append(Violation("promotion", "GATE", f"Release/{bundle.name}", problem))
        detail = {"previous": previous, "violations": [v.key for v in violations]}
        result_text = "REJECTED" if violations else "OK"
        self.run.record("VALIDATE", "promotion_gate", result_text, cmd=cmd, detail=detail)
        return violations

    def _immutability(self) -> list[Violation]:
        """A version, once deployed here, keeps its Release spec and its bundle."""
        bundle = self.bundle
        violations = []
        try:
            live = self.env.get(RELEASES, bundle.name)
            cmd = live.cmd
            if live.json()["spec"] != bundle.spec:
                message = f"{bundle.name} exists here with another spec; publish a new version"
                document = f"Release/{bundle.name}"
                violations.append(Violation("immutability", "RELEASE_SPEC", document, message))
        except NotFound as exc:
            cmd = exc.cmd
        name = bundle_configmap_name(bundle)
        try:
            stored = self.env.get("configmap", name)
            cmd = stored.cmd
            if bundle_digest_of(stored.json()) != bundle.digest:
                message = (
                    f"{bundle.name} was deployed here with other manifests; publish a new version"
                )
                document = f"ConfigMap/{name}"
                violations.append(Violation("immutability", "BUNDLE", document, message))
        except NotFound as exc:
            cmd = exc.cmd
        detail = {"violations": [v.key for v in violations]}
        result_text = "REJECTED" if violations else "OK"
        self.run.record("VALIDATE", "immutability", result_text, cmd=cmd, detail=detail)
        return violations

    # PREPARE --------------------------------------------------------------------------------

    def prepare(self) -> None:
        run, bundle, state = self.run, self.bundle, self.stored.state
        run_id, attempt = run.deployment_id or "", run.attempt or 1
        target = ReleaseRef(bundle.name, bundle.version, bundle.image)
        started = iso_millis(self.ctl.clock())
        in_flight = InFlight(run_id, attempt, target, state.current, "PREPARE", started)
        detail = {
            "target": bundle.name,
            "previous": state.current.release if state.current else None,
            "replaced_in_flight": state.in_flight.deployment_id if state.in_flight else None,
        }
        attempts = {**state.attempts, run_id: attempt}
        run.environment_status = Status.DEPLOYING
        self._save(
            "PREPARE",
            "record_intent",
            detail,
            status=Status.DEPLOYING,
            in_flight=in_flight,
            attempts=attempts,
        )

        try:
            created_cmd, created = self.env.create(bundle_configmap(bundle)).cmd, True
        except AlreadyExists as exc:
            created_cmd, created = exc.cmd, False
        stored_detail: Detail = {"configmap": bundle_configmap_name(bundle), "created": created}
        run.record("PREPARE", "store_bundle", "OK", cmd=created_cmd, detail=stored_detail)

        result = self.env.apply([release_manifest(bundle)])
        release_detail = {"release": bundle.name}
        run.record("PREPARE", "record_release", "OK", cmd=result.cmd, detail=release_detail)
        self._release_phase("PREPARE", "Deploying", "applying the bundle")

    def _release_phase(self, stage: str, phase: str, message: str) -> None:
        status = {
            "phase": phase,
            "attempts": self.run.attempt,
            "deploymentId": self.run.deployment_id,
            "message": message,
            "lastTransitionTime": iso_millis(self.ctl.clock()),
        }
        result = self.env.patch_status(RELEASES, self.bundle.name, {"status": status})
        self.run.record(stage, "release_phase", "OK", cmd=result.cmd, detail={"phase": phase})

    # DEPLOY ---------------------------------------------------------------------------------

    def deploy(self) -> None:
        manifests = workload_manifests(self.bundle)
        result = self.env.apply(manifests)
        detail = {"objects": [describe_object(m) for m in manifests]}
        self.run.record("DEPLOY", "apply", "OK", cmd=result.cmd, detail=detail)
        in_flight = self.stored.state.in_flight
        if in_flight:
            advanced = replace(in_flight, stage="DEPLOY")
            self._save("DEPLOY", "record_stage", {"stage": "DEPLOY"}, in_flight=advanced)

    # VERIFY ---------------------------------------------------------------------------------

    def verify(self) -> Verification:
        self.run.final_release = self.bundle.name
        self._release_phase("VERIFY", "Verifying", "waiting for the rollout")
        names = [deployment["metadata"]["name"] for deployment in self.bundle.deployments]
        verification = verify(
            self.env,
            names,
            self.bundle.timeout_seconds,
            clock=self.ctl.clock,
            sleep=self.ctl.sleep,
            interval=self.ctl.poll_seconds,
            on_poll=self.keeper.renew,
        )
        result = "OK" if verification.ok else "FAIL"
        detail = verification.detail()
        self.run.record("VERIFY", "rollout_status", result, cmd=verification.cmd, detail=detail)
        return verification

    # COMPLETE -------------------------------------------------------------------------------

    def complete(self) -> None:
        in_flight = self.stored.state.in_flight
        if in_flight is None:
            raise RuntimeError("COMPLETE without an intent record")
        target = in_flight.target
        outcome = LastOutcome(in_flight.deployment_id, in_flight.attempt, Outcome.SUCCEEDED)
        self.run.environment_status = Status.HEALTHY
        self.run.final_release = target.release
        detail = {"current": target.release, "status": Status.HEALTHY}
        self._save(
            "COMPLETE",
            "record_outcome",
            detail,
            status=Status.HEALTHY,
            current=target,
            last_known_good=target,
            in_flight=None,
            last_outcome=outcome,
        )
        self._release_phase("COMPLETE", "Succeeded", "rollout complete")
