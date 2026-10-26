from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from deploysim.health import rollout_status, verify
from deploysim.kube import Result

Manifest = dict[str, Any]


def deployment(
    *,
    generation: int = 2,
    observed: int = 2,
    replicas: int = 3,
    updated: int = 3,
    total: int = 3,
    available: int = 3,
    deadline_exceeded: bool = False,
) -> Manifest:
    conditions = []
    if deadline_exceeded:
        conditions.append(
            {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
        )
    return {
        "metadata": {"name": "web", "generation": generation},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": observed,
            "updatedReplicas": updated,
            "replicas": total,
            "availableReplicas": available,
            "conditions": conditions,
        },
    }


@pytest.mark.parametrize(
    ("fields", "complete", "failed", "message"),
    [
        ({}, True, False, "3/3 available"),
        ({"observed": 1}, False, False, "waiting for the spec update to be observed"),
        ({"updated": 1}, False, False, "1 of 3 replicas updated"),
        ({"total": 4}, False, False, "1 old replicas terminating"),
        ({"available": 2}, False, False, "2 of 3 replicas available"),
        ({"available": 0, "deadline_exceeded": True}, False, True, "progress deadline exceeded"),
    ],
    ids=["complete", "unobserved", "updating", "terminating", "unavailable", "deadline"],
)
def test_rollout_status(fields: Manifest, complete: bool, failed: bool, message: str) -> None:
    status = rollout_status(deployment(**fields))

    assert (status.complete, status.failed) == (complete, failed)
    assert status.message.startswith(message)


class FakeCluster:
    """Returns one scripted Deployment per poll and advances a fake clock by 1 s per sleep."""

    def __init__(self, script: list[Manifest]) -> None:
        self.script: Iterator[Manifest] = iter(script)
        self.now = datetime(2026, 10, 26, tzinfo=UTC)
        self.polls_seen = 0

    def get(self, kind: str, name: str) -> Result:
        return Result(("kubectl", "get", kind, name), json.dumps(next(self.script)))

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=1)

    def on_poll(self) -> None:
        self.polls_seen += 1


def run_verify(cluster: FakeCluster, timeout: float) -> Any:
    return verify(
        cluster,
        ["web"],
        timeout,
        clock=cluster.clock,
        sleep=cluster.sleep,
        interval=1,
        on_poll=cluster.on_poll,
    )


def test_verify_waits_until_complete() -> None:
    cluster = FakeCluster([deployment(updated=1), deployment(available=2), deployment()])

    result = run_verify(cluster, timeout=30)

    assert (result.ok, result.reason, result.polls) == (True, "Complete", 3)
    assert result.waited_s == 2
    assert cluster.polls_seen == 3
    assert result.cmd == ("kubectl", "get", "deployment", "web")


def test_verify_fails_fast_on_deadline() -> None:
    cluster = FakeCluster([deployment(updated=1), deployment(available=0, deadline_exceeded=True)])

    result = run_verify(cluster, timeout=30)

    assert (result.ok, result.reason, result.polls) == (False, "ProgressDeadlineExceeded", 2)


def test_verify_times_out() -> None:
    cluster = FakeCluster([deployment(available=0)] * 10)

    result = run_verify(cluster, timeout=3)

    assert (result.ok, result.reason, result.polls) == (False, "Timeout", 4)
    assert result.detail()["deployments"][0]["message"] == "0 of 3 replicas available"
