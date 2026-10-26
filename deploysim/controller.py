"""deploy and recover: VALIDATE, LOCK, RECOVER, cluster validation, PREPARE, DEPLOY, VERIFY,
then COMPLETE or ROLLBACK."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from deploysim.audit import AuditError, AuditLog, Entry
from deploysim.bundle import Bundle, Violation, canonical_json, version_key
from deploysim.clock import iso_millis, utcnow
from deploysim.errors import EnvironmentProblem
from deploysim.health import Verification, rollout_status, verify
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
from deploysim.outcomes import ExitCode, Outcome
from deploysim.paths import Paths
from deploysim.state import InFlight, LastOutcome, ReleaseRef, StateStore, Status, Stored
from deploysim.validate import validate_file

ENVIRONMENTS = ("dev", "staging", "prod")
RELEASES = "releases.deploysim.io"
DIGEST_ANNOTATION = "deploysim.io/bundle-digest"
FAULT_ANNOTATION = "deploysim.io/fault"
BUNDLE_KEY = "objects.json"
FAULTS = (
    "unhealthy",
    "unhealthy-rollback",
    "crash-after-prepare",
    "crash-after-deploy",
    "crash-during-rollback",
)

Detail = dict[str, Any]


class LeaseLost(EnvironmentProblem):
    """Another run took the Lease over; this run stopped writing."""


def deployment_id(bundle: Bundle, environment: str) -> str:
    """dep- plus 12 hex of sha256(app | environment | Release spec | bundle digest)."""
    material = "|".join([bundle.app, environment, canonical_json(bundle.spec), bundle.digest])
    return "dep-" + hashlib.sha256(material.encode()).hexdigest()[:12]


def stored_bundle_name(app: str, version: str) -> str:
    return f"deploysim-{app}-{version}"


def bundle_configmap_name(bundle: Bundle) -> str:
    return stored_bundle_name(bundle.app, bundle.version)


def unrestorable(app: str, ref: ReleaseRef) -> str:
    return (
        f"cannot restore {ref.release}: ConfigMap {stored_bundle_name(app, ref.version)} is "
        f"missing; redeploy {ref.release}, or a fixed version, with --allow-degraded"
    )


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


def workload_manifests(
    objects: Iterable[Manifest], app: str, fault: str | None = None
) -> list[Manifest]:
    """Copies with deploysim's labels; fault, if set, annotates every Deployment pod template."""
    manifests = []
    for obj in objects:
        manifest = copy.deepcopy(obj)
        metadata = manifest.setdefault("metadata", {})
        metadata["labels"] = {**(metadata.get("labels") or {}), **managed_labels(app)}
        if fault and manifest.get("kind") == "Deployment":
            template = manifest["spec"]["template"].setdefault("metadata", {})
            annotations = template.get("annotations") or {}
            template["annotations"] = {**annotations, FAULT_ANNOTATION: fault}
        manifests.append(manifest)
    return manifests


def deployment_names(objects: Iterable[Manifest]) -> list[str]:
    return [obj["metadata"]["name"] for obj in objects if obj.get("kind") == "Deployment"]


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
    inject_fault: str | None = None
    holder: str = field(default_factory=default_holder)

    @property
    def target_fault(self) -> str | None:
        unhealthy_target = ("unhealthy", "unhealthy-rollback", "crash-during-rollback")
        return "unhealthy" if self.inject_fault in unhealthy_target else None

    @property
    def rollback_fault(self) -> str | None:
        return "unhealthy" if self.inject_fault == "unhealthy-rollback" else None


@dataclass(frozen=True)
class RecoverRequest:
    app: str
    environment: str
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
        self.recovery: Detail = {
            "performed": False,
            "interrupted": None,
            "to": None,
            "result": None,
        }
        self.rollback: Detail = {"performed": False, "to": None, "result": None}
        self.drift_corrected = False
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
            "recovery": self.recovery,
            "rollback": self.rollback,
            "drift_corrected": self.drift_corrected,
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

    def recover(self, request: RecoverRequest) -> DeployReport:
        return Recover(self, request).execute()


class LeaseKeeper:
    """Holds this run's Lease: renewed on every rollout poll, released at the end."""

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


class Invocation:
    """One CLI command: maps environment problems and unexpected errors to exit 5, and always
    releases the Lease. An intent record left behind is resolved by the next run."""

    def __init__(self, controller: Controller, environment: str) -> None:
        self.ctl = controller
        self.run = Run(controller.audit, controller.progress, environment, controller.clock())
        self.keeper: LeaseKeeper | None = None

    def execute(self) -> DeployReport:
        try:
            ending = self._execute()
        except (EnvironmentProblem, KubeError) as exc:
            cmd = exc.cmd if isinstance(exc, KubeError) else None
            self.run.record(self.run.stage, "error", "ERROR", cmd=cmd, detail={"error": str(exc)})
            ending = Ending(Outcome.ENVIRONMENT_ERROR, error=str(exc))
        except AuditError:
            raise
        except Exception as exc:
            error = f"unexpected {type(exc).__name__}: {exc}"
            traceback.print_exc(file=self.run.progress)
            self.run.record(self.run.stage, "error", "ERROR", detail={"error": error})
            ending = Ending(Outcome.ENVIRONMENT_ERROR, error=error)
        finally:
            if self.keeper:
                self.keeper.release(self.run)
        return DeployReport(ending.outcome, self.run.summary(ending, self.ctl.clock()))

    def _execute(self) -> Ending:
        raise NotImplementedError

    def _lock(self, env: Environment, app: str, holder: str, seconds: int) -> Ending | None:
        try:
            lease = acquire(env, app, holder, seconds, self.ctl.clock())
        except Locked as exc:
            expires = iso_millis(exc.expires_at) if exc.expires_at else None
            detail: Detail = {"holder": exc.holder, "expires_at": expires}
            self.run.record("LOCK", "acquire_lease", "LOCKED", detail=detail)
            return Ending(Outcome.LOCKED, error=f"lease {exc}")
        detail = {"holder": lease.holder, "took_over_from": lease.took_over_from}
        self.run.record("LOCK", "acquire_lease", "OK", cmd=lease.cmd, detail=detail)
        self.keeper = LeaseKeeper(lease, self.ctl.clock)
        return None


class Deploy(Invocation):
    """deploysim deploy."""

    def __init__(self, controller: Controller, request: DeployRequest) -> None:
        super().__init__(controller, request.environment)
        self.request = request

    def _execute(self) -> Ending:
        request = self.request
        bundle = self._validate_offline()
        if isinstance(bundle, Ending):
            return bundle
        kubectl = Kubectl.connect(self.ctl.paths, self.ctl.kubeconfig)
        env = kubectl.environment(request.environment)
        locked = self._lock(env, bundle.app, request.holder, request.lease_seconds)
        if locked:
            return locked
        assert self.keeper is not None

        session = Session.open(self.ctl, self.run, env, self.keeper, request, kubectl, bundle)
        failed_recovery = session.recover()
        if failed_recovery:
            return failed_recovery
        rejected = session.validate_cluster()
        if rejected:
            return rejected
        unchanged = session.desired_state()
        if unchanged:
            return unchanged
        session.prepare()
        session.crash_point("after-prepare")
        session.deploy()
        session.crash_point("after-deploy")
        verification = session.verify()
        if not verification.ok:
            return session.rollback(verification)
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


class Recover(Invocation):
    """deploysim recover: resolves an interrupted deployment without starting a new one."""

    def __init__(self, controller: Controller, request: RecoverRequest) -> None:
        super().__init__(controller, request.environment)
        self.request = request
        self.run.app = request.app

    def _execute(self) -> Ending:
        request = self.request
        if request.environment not in ENVIRONMENTS:
            allowed = ", ".join(ENVIRONMENTS)
            violation = Violation("environment", "UNKNOWN", None, f"must be one of {allowed}")
            return Ending(Outcome.REJECTED, (violation,))
        env = Kubectl.connect(self.ctl.paths, self.ctl.kubeconfig).environment(request.environment)
        locked = self._lock(env, request.app, request.holder, request.lease_seconds)
        if locked:
            return locked
        assert self.keeper is not None

        store, stored = Operator.load(self.run, env, request.app)
        operator = Operator(self.ctl, self.run, env, request.app, self.keeper, store, stored)
        if stored.state.in_flight is None:
            self.run.record("RECOVER", "find_intent", "NO_OP", detail={"in_flight": None})
            return Ending(Outcome.NO_OP)
        failed_recovery = operator.recover()
        return failed_recovery or Ending(Outcome.RECOVERED)


@dataclass
class Operator:
    """Writes to one environment under the Lease, recording every step. Holds the stages
    shared by deploy and recover: restoring a release, removing objects, recovery."""

    ctl: Controller
    run: Run
    env: Environment
    app: str
    keeper: LeaseKeeper
    store: StateStore
    stored: Stored

    @staticmethod
    def load(run: Run, env: Environment, app: str) -> tuple[StateStore, Stored]:
        """Loads the environment's state and records it."""
        store = StateStore(env, app)
        stored = store.load()
        state = stored.state
        run.from_release = state.current.release if state.current else None
        run.final_release = run.from_release
        run.environment_status = state.status
        in_flight = state.in_flight.deployment_id if state.in_flight else None
        detail = {"status": state.status, "current": run.from_release, "in_flight": in_flight}
        run.record("VALIDATE", "load_state", "OK", cmd=stored.cmd, detail=detail)
        return store, stored

    def _save(self, stage: str, action: str, detail: Detail, **changes: Any) -> None:
        state = replace(self.stored.state, **changes)
        self.stored = self.store.save(self.stored, state, self.ctl.clock())
        self.run.record(stage, action, "OK", cmd=self.stored.cmd, detail=detail)

    def _advance(self, stage: str) -> None:
        in_flight = self.stored.state.in_flight
        if in_flight:
            advanced = replace(in_flight, stage=stage)
            self._save(stage, "record_stage", {"stage": stage}, in_flight=advanced)

    def _mark_release(
        self,
        stage: str,
        release_name: str,
        phase: str,
        message: str,
        attempt: int | None,
        deployment_id: str | None,
    ) -> None:
        status = {
            "phase": phase,
            "attempts": attempt,
            "deploymentId": deployment_id,
            "message": message,
            "lastTransitionTime": iso_millis(self.ctl.clock()),
        }
        result = self.env.patch_status(RELEASES, release_name, {"status": status})
        detail = {"release": release_name, "phase": phase}
        self.run.record(stage, "release_phase", "OK", cmd=result.cmd, detail=detail)

    def _await_rollout(self, stage: str, names: list[str], timeout: float) -> Verification:
        verification = verify(
            self.env,
            names,
            timeout,
            clock=self.ctl.clock,
            sleep=self.ctl.sleep,
            interval=self.ctl.poll_seconds,
            on_poll=self.keeper.renew,
        )
        result = "OK" if verification.ok else "FAIL"
        detail = verification.detail()
        self.run.record(stage, "rollout_status", result, cmd=verification.cmd, detail=detail)
        return verification

    def _stored_objects(self, stage: str, ref: ReleaseRef) -> list[Manifest] | None:
        """The objects stored for ref, or None if its bundle ConfigMap does not exist."""
        name = stored_bundle_name(self.app, ref.version)
        try:
            stored = self.env.get("configmap", name)
        except NotFound as exc:
            missing: Detail = {"configmap": name}
            self.run.record(stage, "load_bundle", "MISSING", cmd=exc.cmd, detail=missing)
            return None
        objects: list[Manifest] = json.loads(stored.json()["data"][BUNDLE_KEY])
        detail = {"configmap": name, "objects": len(objects)}
        self.run.record(stage, "load_bundle", "OK", cmd=stored.cmd, detail=detail)
        return objects

    def _reapply(
        self, stage: str, ref: ReleaseRef, replaced: list[Manifest], fault: str | None
    ) -> tuple[list[str], int] | None:
        """Applies ref's stored objects, then deletes the objects in replaced that ref lacks.
        Returns ref's Deployments and health check timeout, or None if ref's bundle is gone."""
        objects = self._stored_objects(stage, ref)
        if objects is None:
            return None
        release_spec = self.env.get(RELEASES, ref.release).json()["spec"]
        manifests = workload_manifests(objects, self.app, fault)
        result = self.env.apply(manifests)
        detail = {
            "to": ref.release,
            "objects": [describe_object(m) for m in manifests],
            "injected_fault": fault,
        }
        self.run.record(stage, "apply", "OK", cmd=result.cmd, detail=detail)
        kept = {describe_object(obj) for obj in objects}
        self._remove(stage, [obj for obj in replaced if describe_object(obj) not in kept])
        return deployment_names(objects), int(release_spec["healthCheck"]["timeoutSeconds"])

    def _remove(self, stage: str, objects: list[Manifest]) -> None:
        for manifest in workload_manifests(objects, self.app):
            kind, name = manifest["kind"], manifest["metadata"]["name"]
            result = self.env.delete(kind, name, wait=True)
            removed: Detail = {"object": f"{kind}/{name}"}
            self.run.record(stage, "delete", "OK", cmd=result.cmd, detail=removed)

    def _finish(self, stage: str, status: Status, current: ReleaseRef | None, outcome: str) -> None:
        """Clears in_flight and records the interrupted or rolled-back attempt's outcome."""
        in_flight = self.stored.state.in_flight
        if in_flight is None:
            raise RuntimeError(f"{stage} without an intent record")
        last = LastOutcome(in_flight.deployment_id, in_flight.attempt, outcome)
        detail = {"current": current.release if current else None, "status": status}
        self._save(
            stage,
            "record_outcome",
            detail,
            status=status,
            current=current,
            in_flight=None,
            last_outcome=last,
        )
        self.run.environment_status = status
        self.run.final_release = current.release if current else None

    # RECOVER --------------------------------------------------------------------------------

    def recover(self) -> Ending | None:
        """A leftover intent record means its run died. Restores that record's previous
        release and deletes the target's objects it lacks, or removes the target's objects if
        there was none, and marks the interrupted Release Interrupted. Returns an Ending only
        if the restore failed."""
        in_flight = self.stored.state.in_flight
        if in_flight is None:
            return None
        target, previous = in_flight.target, in_flight.previous
        detail = {
            "interrupted": in_flight.deployment_id,
            "attempt": in_flight.attempt,
            "stage": in_flight.stage,
            "target": target.release,
            "previous": previous.release if previous else None,
        }
        self.run.record("RECOVER", "find_intent", "FOUND", detail=detail)

        replaced = self._stored_objects("RECOVER", target) or []
        current, restored = previous, previous.release if previous else "nothing"
        if previous is None:
            self._remove("RECOVER", replaced)
            ok, status, reason = True, Status.EMPTY, "Complete"
        elif (reapplied := self._reapply("RECOVER", previous, replaced, None)) is None:
            ok, status, reason = False, Status.DEGRADED, unrestorable(self.app, previous)
            current, restored = target, "nothing"
        else:
            check = self._await_rollout("RECOVER", *reapplied)
            ok, reason = check.ok, check.reason
            status = Status.HEALTHY if ok else Status.DEGRADED
        self._finish("RECOVER", status, current, "INTERRUPTED")

        message = f"interrupted at {in_flight.stage}; restored {restored}"
        try:
            self._mark_release(
                "RECOVER",
                target.release,
                "Interrupted",
                message,
                in_flight.attempt,
                in_flight.deployment_id,
            )
        except NotFound as exc:
            self.run.record("RECOVER", "release_phase", "MISSING", cmd=exc.cmd)
        self.run.recovery = {
            "performed": True,
            "interrupted": in_flight.deployment_id,
            "to": previous.release if previous else None,
            "result": "SUCCEEDED" if ok else "FAILED",
        }
        if ok:
            return None
        error = f"recovery to {detail['previous']} failed: {reason}"
        return Ending(Outcome.ROLLBACK_FAILED, error=error)


@dataclass
class Session(Operator):
    """The deploy stages that run while this run holds the Lease."""

    request: DeployRequest
    kubectl: Kubectl
    bundle: Bundle

    @classmethod
    def open(
        cls,
        ctl: Controller,
        run: Run,
        env: Environment,
        keeper: LeaseKeeper,
        request: DeployRequest,
        kubectl: Kubectl,
        bundle: Bundle,
    ) -> Session:
        store, stored = Operator.load(run, env, bundle.app)
        return cls(ctl, run, env, bundle.app, keeper, store, stored, request, kubectl, bundle)

    def crash_point(self, point: str) -> None:
        """--inject-fault crash-<point>: exit at once, leaving the Lease and intent record."""
        fault = f"crash-{point}"
        if self.request.inject_fault != fault:
            return
        self.run.record(self.run.stage, "inject_crash", "CRASH", detail={"fault": fault})
        self.run.progress.flush()
        os._exit(ExitCode.SIMULATED_CRASH)

    def _phase(self, stage: str, phase: str, message: str) -> None:
        run = self.run
        self._mark_release(stage, self.bundle.name, phase, message, run.attempt, run.deployment_id)

    # VALIDATE (cluster) ---------------------------------------------------------------------

    def validate_cluster(self) -> Ending | None:
        violations = [*self._degraded_gate(), *self._promotion_gate(), *self._immutability()]
        if violations:
            return Ending(Outcome.REJECTED, tuple(violations))
        manifests = [
            release_manifest(self.bundle),
            bundle_configmap(self.bundle),
            *workload_manifests(self.bundle.objects, self.bundle.app),
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

    # Desired state ---------------------------------------------------------------------------

    def desired_state(self) -> Ending | None:
        """NO_OP when the target is current, applying it would change nothing, and every
        Deployment is rolled out. A current target that drifted or is unhealthy is redeployed."""
        state, bundle = self.stored.state, self.bundle
        current = state.current
        if (
            current is None
            or current.release != bundle.name
            or state.in_flight is not None
            or state.status != Status.HEALTHY
        ):
            detail: Detail = {"target_is_current": False}
            self.run.record("VALIDATE", "desired_state", "DEPLOY", detail=detail)
            return None

        manifests = workload_manifests(bundle.objects, bundle.app, self.request.target_fault)
        drift = self.env.drift(manifests)
        rollouts = []
        for name in deployment_names(bundle.objects):
            rollouts.append(rollout_status(self.env.get("deployment", name).json()))
        healthy = all(status.complete for status in rollouts)
        detail = {
            "target_is_current": True,
            "drift": list(drift.changed),
            "rollout": [status.to_dict() for status in rollouts],
        }
        if drift.changed or not healthy:
            self.run.drift_corrected = True
            self.run.record("VALIDATE", "desired_state", "DRIFT", cmd=drift.cmds[0], detail=detail)
            return None
        self.run.record("VALIDATE", "desired_state", "NO_OP", cmd=drift.cmds[0], detail=detail)
        return Ending(Outcome.NO_OP)

    # PREPARE --------------------------------------------------------------------------------

    def prepare(self) -> None:
        run, bundle, state = self.run, self.bundle, self.stored.state
        run_id = run.deployment_id or ""
        attempt = state.attempts.get(run_id, 0) + 1
        run.attempt = attempt
        target = ReleaseRef(bundle.name, bundle.version, bundle.image)
        started = iso_millis(self.ctl.clock())
        previous = state.last_known_good
        in_flight = InFlight(run_id, attempt, target, previous, "PREPARE", started)
        current = state.current
        detail = {
            "target": bundle.name,
            "previous": previous.release if previous else None,
            "replaced_in_flight": state.in_flight.deployment_id if state.in_flight else None,
            "downgrade": current is not None
            and version_key(bundle.version) < version_key(current.version),
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
        self._phase("PREPARE", "Deploying", "applying the bundle")

    # DEPLOY ---------------------------------------------------------------------------------

    def deploy(self) -> None:
        fault = self.request.target_fault
        manifests = workload_manifests(self.bundle.objects, self.bundle.app, fault)
        result = self.env.apply(manifests)
        detail = {"objects": [describe_object(m) for m in manifests], "injected_fault": fault}
        self.run.record("DEPLOY", "apply", "OK", cmd=result.cmd, detail=detail)
        self._advance("DEPLOY")

    # VERIFY ---------------------------------------------------------------------------------

    def verify(self) -> Verification:
        self.run.final_release = self.bundle.name
        self._phase("VERIFY", "Verifying", "waiting for the rollout")
        names = deployment_names(self.bundle.objects)
        return self._await_rollout("VERIFY", names, self.bundle.timeout_seconds)

    # ROLLBACK -------------------------------------------------------------------------------

    def rollback(self, failed: Verification) -> Ending:
        """Restores last_known_good, deletes the target's objects it lacks, and verifies it;
        on a first deploy, removes the objects."""
        in_flight = self.stored.state.in_flight
        if in_flight is None:
            raise RuntimeError("ROLLBACK without an intent record")
        self._advance("ROLLBACK")
        reason = f"health check failed: {failed.reason}"
        previous = in_flight.previous
        if previous is None:
            self._remove("ROLLBACK", list(self.bundle.objects))
            self.crash_point("during-rollback")
            self._conclude(Status.EMPTY, None, Outcome.ROLLED_BACK, "nothing to restore; removed")
            return Ending(Outcome.ROLLED_BACK, error=f"{reason}; first deployment removed")

        objects, fault = list(self.bundle.objects), self.request.rollback_fault
        reapplied = self._reapply("ROLLBACK", previous, objects, fault)
        if reapplied is None:
            message = unrestorable(self.app, previous)
            self._conclude(Status.DEGRADED, in_flight.target, Outcome.ROLLBACK_FAILED, message)
            return Ending(Outcome.ROLLBACK_FAILED, error=f"{reason}; {message}")
        self.crash_point("during-rollback")
        restored = self._await_rollout("ROLLBACK", *reapplied)
        if restored.ok:
            message = f"rolled back to {previous.release}"
            self._conclude(Status.HEALTHY, previous, Outcome.ROLLED_BACK, message)
            return Ending(Outcome.ROLLED_BACK, error=f"{reason}; {message}")
        message = f"rollback to {previous.release} failed: {restored.reason}"
        self._conclude(Status.DEGRADED, previous, Outcome.ROLLBACK_FAILED, message)
        return Ending(Outcome.ROLLBACK_FAILED, error=f"{reason}; {message}")

    def _conclude(
        self, status: Status, current: ReleaseRef | None, outcome: Outcome, message: str
    ) -> None:
        """current is what the environment now runs; the rollback's target is last_known_good."""
        in_flight = self.stored.state.in_flight
        restoring = in_flight.previous if in_flight else None
        self._finish("ROLLBACK", status, current, outcome)
        phase = "RolledBack" if outcome == Outcome.ROLLED_BACK else "RollbackFailed"
        self._phase("ROLLBACK", phase, message)
        self.run.rollback = {
            "performed": True,
            "to": restoring.release if restoring else None,
            "result": "SUCCEEDED" if outcome == Outcome.ROLLED_BACK else "FAILED",
        }

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
        self._phase("COMPLETE", "Succeeded", "rollout complete")
