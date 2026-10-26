"""VERIFY: waits for every Deployment in a bundle to finish rolling out."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol

from deploysim.kube import Manifest, Result


class Getter(Protocol):
    def get(self, kind: str, name: str) -> Result: ...


@dataclass(frozen=True)
class RolloutStatus:
    name: str
    complete: bool
    failed: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rollout_status(deployment: Manifest) -> RolloutStatus:
    """Same decision as kubectl rollout status, plus failure on ProgressDeadlineExceeded."""
    name = deployment["metadata"]["name"]
    status = deployment.get("status") or {}
    if status.get("observedGeneration", 0) < deployment["metadata"].get("generation", 0):
        return RolloutStatus(name, False, False, "waiting for the spec update to be observed")

    replicas = int(deployment["spec"].get("replicas", 1))
    updated = int(status.get("updatedReplicas", 0))
    total = int(status.get("replicas", 0))
    available = int(status.get("availableReplicas", 0))
    for condition in status.get("conditions") or []:
        reason = condition.get("reason")
        if condition["type"] == "Progressing" and reason == "ProgressDeadlineExceeded":
            message = (
                f"progress deadline exceeded: {updated} of {replicas} replicas updated, "
                f"{available} available"
            )
            return RolloutStatus(name, False, True, message)
    if updated < replicas:
        return RolloutStatus(name, False, False, f"{updated} of {replicas} replicas updated")
    if total > updated:
        return RolloutStatus(name, False, False, f"{total - updated} old replicas terminating")
    if available < updated:
        return RolloutStatus(name, False, False, f"{available} of {updated} replicas available")
    return RolloutStatus(name, True, False, f"{available}/{replicas} available")


@dataclass(frozen=True)
class Verification:
    """reason: Complete, ProgressDeadlineExceeded or Timeout."""

    ok: bool
    reason: str
    deployments: tuple[RolloutStatus, ...]
    waited_s: float
    polls: int
    cmd: tuple[str, ...] | None

    def detail(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "waited_s": round(self.waited_s, 1),
            "polls": self.polls,
            "deployments": [status.to_dict() for status in self.deployments],
        }


def verify(
    env: Getter,
    deployments: list[str],
    timeout_seconds: float,
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
    interval: float,
    on_poll: Callable[[], None],
) -> Verification:
    """Polls until every Deployment is complete, one fails, or timeout_seconds pass.
    on_poll runs after every poll; the controller renews the Lease there."""
    started = clock()
    polls = 0
    while True:
        polls += 1
        cmd = None
        statuses = []
        for name in deployments:
            result = env.get("deployment", name)
            cmd = result.cmd
            statuses.append(rollout_status(result.json()))
        on_poll()
        waited = (clock() - started).total_seconds()

        reason = None
        if any(status.failed for status in statuses):
            reason = "ProgressDeadlineExceeded"
        elif all(status.complete for status in statuses):
            reason = "Complete"
        elif waited >= timeout_seconds:
            reason = "Timeout"
        if reason:
            return Verification(reason == "Complete", reason, tuple(statuses), waited, polls, cmd)
        sleep(interval)
