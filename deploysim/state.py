"""Controller state per app and environment in ConfigMap deploysim-<app>; saves are CAS writes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from deploysim.clock import iso_millis
from deploysim.kube import AlreadyExists, Conflict, Environment, NotFound, managed_labels

STATE_KEY = "state.json"


class Status(StrEnum):
    EMPTY = "EMPTY"
    HEALTHY = "HEALTHY"
    DEPLOYING = "DEPLOYING"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class ReleaseRef:
    release: str
    version: str
    image: str


@dataclass(frozen=True)
class InFlight:
    deployment_id: str
    attempt: int
    target: ReleaseRef
    previous: ReleaseRef | None
    stage: str
    started_at: str


@dataclass(frozen=True)
class Outcome:
    deployment_id: str
    attempt: int
    outcome: str


@dataclass(frozen=True)
class State:
    """Field order is the order in state.json."""

    app: str
    environment: str
    status: Status = Status.EMPTY
    current: ReleaseRef | None = None
    last_known_good: ReleaseRef | None = None
    in_flight: InFlight | None = None
    last_outcome: Outcome | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        in_flight = data.get("in_flight")
        return cls(
            app=data["app"],
            environment=data["environment"],
            status=Status(data["status"]),
            current=_ref(data.get("current")),
            last_known_good=_ref(data.get("last_known_good")),
            in_flight=None
            if in_flight is None
            else InFlight(
                deployment_id=in_flight["deployment_id"],
                attempt=in_flight["attempt"],
                target=ReleaseRef(**in_flight["target"]),
                previous=_ref(in_flight.get("previous")),
                stage=in_flight["stage"],
                started_at=in_flight["started_at"],
            ),
            last_outcome=Outcome(**data["last_outcome"]) if data.get("last_outcome") else None,
            attempts=dict(data.get("attempts") or {}),
            updated_at=data.get("updated_at"),
        )


def _ref(data: dict[str, Any] | None) -> ReleaseRef | None:
    return ReleaseRef(**data) if data else None


def state_name(app: str) -> str:
    return f"deploysim-{app}"


@dataclass(frozen=True)
class Stored:
    """A state as read or written, with the resourceVersion the next save must match."""

    state: State
    resource_version: str | None
    cmd: tuple[str, ...] | None


class StateStore:
    def __init__(self, env: Environment, app: str) -> None:
        self.env = env
        self.app = app

    def load(self) -> Stored:
        """An empty state when the ConfigMap does not exist yet."""
        try:
            result = self.env.get("configmap", state_name(self.app))
        except NotFound:
            return Stored(State(self.app, self.env.name), None, None)
        configmap = result.json()
        state = State.from_dict(json.loads(configmap["data"][STATE_KEY]))
        return Stored(state, configmap["metadata"]["resourceVersion"], result.cmd)

    def save(self, stored: Stored, state: State, now: datetime) -> Stored:
        """Raises Conflict if the ConfigMap changed since stored was read."""
        state = replace(state, updated_at=iso_millis(now))
        manifest: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": state_name(self.app), "labels": managed_labels(self.app)},
            "data": {STATE_KEY: json.dumps(state.to_dict(), indent=2)},
        }
        if stored.resource_version is None:
            try:
                result = self.env.create(manifest)
            except AlreadyExists as exc:
                raise Conflict(exc.cmd, exc.stderr) from exc
        else:
            manifest["metadata"]["resourceVersion"] = stored.resource_version
            result = self.env.replace(manifest)
        return Stored(state, result.json()["metadata"]["resourceVersion"], result.cmd)
