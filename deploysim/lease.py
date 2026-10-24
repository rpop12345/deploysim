"""Per app and environment lock on a coordination.k8s.io Lease; every write is compare-and-swap."""

from __future__ import annotations

import copy
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from deploysim.clock import iso_micros
from deploysim.kube import AlreadyExists, Conflict, Environment, Manifest, managed_labels


class Locked(Exception):
    """Another live run holds the Lease."""

    def __init__(self, holder: str, expires_at: datetime | None) -> None:
        until = f" until {expires_at:%Y-%m-%dT%H:%M:%SZ}" if expires_at else ""
        super().__init__(f"held by {holder}{until}")
        self.holder = holder
        self.expires_at = expires_at


def lease_name(app: str) -> str:
    return f"deploysim-{app}"


def default_holder() -> str:
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class HeldLease:
    """A Lease this run holds; obj is the last version written, carrying its resourceVersion."""

    env: Environment
    obj: Manifest
    took_over_from: str | None
    cmd: tuple[str, ...]

    @property
    def holder(self) -> str:
        return str(self.obj["spec"]["holderIdentity"])

    @property
    def transitions(self) -> int:
        return int(self.obj["spec"].get("leaseTransitions") or 0)


def expires_at(spec: Manifest) -> datetime | None:
    renewed = spec.get("renewTime") or spec.get("acquireTime")
    if not renewed or "leaseDurationSeconds" not in spec:
        return None
    return datetime.fromisoformat(renewed) + timedelta(seconds=int(spec["leaseDurationSeconds"]))


def acquire(env: Environment, app: str, holder: str, seconds: int, now: datetime) -> HeldLease:
    """Creates the Lease, or takes over one that is released or expired. Raises Locked otherwise."""
    name = lease_name(app)
    spec = {
        "holderIdentity": holder,
        "leaseDurationSeconds": seconds,
        "acquireTime": iso_micros(now),
        "renewTime": iso_micros(now),
        "leaseTransitions": 0,
    }
    manifest = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {"name": name, "labels": managed_labels(app)},
        "spec": spec,
    }
    try:
        result = env.create(manifest)
        return HeldLease(env, result.json(), None, result.cmd)
    except AlreadyExists:
        pass

    current = env.get("lease", name).json()
    current_spec = current.get("spec") or {}
    previous = current_spec.get("holderIdentity")
    expiry = expires_at(current_spec)
    if previous and expiry is not None and now < expiry:
        raise Locked(previous, expiry)

    transitions = int(current_spec.get("leaseTransitions") or 0) + 1
    current["spec"] = {**spec, "leaseTransitions": transitions}
    try:
        result = env.replace(current)
    except Conflict as exc:
        winner = (env.get("lease", name).json().get("spec") or {}).get("holderIdentity")
        raise Locked(winner or "another run", None) from exc
    return HeldLease(env, result.json(), previous, result.cmd)


def renew(lease: HeldLease, now: datetime) -> HeldLease:
    """Raises Conflict if another run took the Lease over: this run must stop writing."""
    obj = copy.deepcopy(lease.obj)
    obj["spec"]["renewTime"] = iso_micros(now)
    result = lease.env.replace(obj)
    return HeldLease(lease.env, result.json(), lease.took_over_from, result.cmd)


def release(lease: HeldLease, now: datetime) -> tuple[str, ...]:
    """Clears the holder so the next run acquires at once. Raises Conflict if already taken over."""
    obj = copy.deepcopy(lease.obj)
    obj["spec"].pop("holderIdentity", None)
    obj["spec"]["renewTime"] = iso_micros(now)
    return lease.env.replace(obj).cmd
