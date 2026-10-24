from __future__ import annotations

import re
import secrets
from datetime import timedelta

import pytest

from deploysim.clock import utcnow
from deploysim.errors import UnsafeTarget
from deploysim.kube import Conflict, Environment, Invalid, Kubectl
from deploysim.lease import Locked, acquire, release, renew
from deploysim.paths import Paths
from deploysim.state import InFlight, Outcome, ReleaseRef, State, StateStore, Status
from tests.e2e import support
from tests.e2e.support import REPO, deployment, image

pytestmark = pytest.mark.e2e

SECONDS = 5


@pytest.fixture(scope="module")
def cluster() -> Kubectl:
    return Kubectl.connect(Paths(REPO))


@pytest.fixture
def dev(cluster: Kubectl, kubectl: support.Kubectl) -> Environment:
    return cluster.environment("dev")


def test_guard_refuses_unlabelled_namespace(cluster: Kubectl, kubectl: support.Kubectl) -> None:
    name = f"t-{secrets.token_hex(3)}-unlabelled"
    expected = re.escape("not labelled deploysim.io/simulator=true")
    kubectl.run("create", "namespace", name)
    try:
        with pytest.raises(UnsafeTarget, match=expected):
            cluster.environment(name)
    finally:
        kubectl.run("delete", "namespace", name, "--wait=false")


def test_guard_refuses_missing_namespace(cluster: Kubectl) -> None:
    with pytest.raises(UnsafeTarget, match="does not exist"):
        cluster.environment(f"t-{secrets.token_hex(3)}-missing")


def test_lease_exclusive(dev: Environment, app: str) -> None:
    now = utcnow()
    held = acquire(dev, app, "run-a", SECONDS, now)

    with pytest.raises(Locked) as locked:
        acquire(dev, app, "run-b", SECONDS, now + timedelta(seconds=1))

    assert held.holder == "run-a"
    assert held.took_over_from is None
    assert locked.value.holder == "run-a"


def test_lease_takeover_after_expiry(dev: Environment, app: str) -> None:
    now = utcnow()
    acquire(dev, app, "run-a", SECONDS, now)

    taken = acquire(dev, app, "run-b", SECONDS, now + timedelta(seconds=SECONDS + 1))

    assert taken.holder == "run-b"
    assert taken.took_over_from == "run-a"
    assert taken.transitions == 1


def test_lease_renew_extends(dev: Environment, app: str) -> None:
    now = utcnow()
    held = acquire(dev, app, "run-a", SECONDS, now)

    renew(held, now + timedelta(seconds=SECONDS - 1))

    with pytest.raises(Locked):
        acquire(dev, app, "run-b", SECONDS, now + timedelta(seconds=SECONDS + 1))


def test_lease_fencing(dev: Environment, app: str) -> None:
    now = utcnow()
    stale = acquire(dev, app, "run-a", SECONDS, now)
    acquire(dev, app, "run-b", SECONDS, now + timedelta(seconds=SECONDS + 1))

    with pytest.raises(Conflict):
        renew(stale, now + timedelta(seconds=SECONDS + 2))
    with pytest.raises(Conflict):
        release(stale, now + timedelta(seconds=SECONDS + 2))
    assert dev.get("lease", f"deploysim-{app}").json()["spec"]["holderIdentity"] == "run-b"


def test_lease_release(dev: Environment, app: str) -> None:
    now = utcnow()
    held = acquire(dev, app, "run-a", SECONDS, now)

    release(held, now)
    taken = acquire(dev, app, "run-b", SECONDS, now)

    assert taken.holder == "run-b"
    assert taken.took_over_from is None


def full_state(app: str) -> State:
    good = ReleaseRef(f"{app}-1.0.0", "1.0.0", image(app, "1"))
    target = ReleaseRef(f"{app}-1.1.0", "1.1.0", image(app, "2"))
    return State(
        app=app,
        environment="dev",
        status=Status.DEPLOYING,
        current=good,
        last_known_good=good,
        in_flight=InFlight("dep-b", 2, target, good, "VERIFY", "2026-10-24T10:00:00.000Z"),
        last_outcome=Outcome("dep-b", 1, "ROLLED_BACK"),
        attempts={"dep-a": 1, "dep-b": 2},
    )


def test_state_roundtrip(dev: Environment, app: str) -> None:
    store = StateStore(dev, app)
    empty = store.load()

    saved = store.save(empty, full_state(app), utcnow())
    loaded = store.load()

    assert empty.resource_version is None
    assert empty.state == State(app, "dev")
    assert loaded.state == saved.state
    assert loaded.resource_version == saved.resource_version
    assert loaded.state.updated_at is not None


def test_state_cas(dev: Environment, app: str) -> None:
    store = StateStore(dev, app)
    store.save(store.load(), State(app, "dev"), utcnow())
    first, second = store.load(), store.load()
    winner = State(app, "dev", status=Status.HEALTHY)

    store.save(first, winner, utcnow())
    with pytest.raises(Conflict):
        store.save(second, State(app, "dev", status=Status.DEGRADED), utcnow())

    assert store.load().state.status == Status.HEALTHY


def test_state_create_race(dev: Environment, app: str) -> None:
    store = StateStore(dev, app)
    first, second = store.load(), store.load()

    store.save(first, State(app, "dev", status=Status.HEALTHY), utcnow())

    with pytest.raises(Conflict):
        store.save(second, State(app, "dev", status=Status.DEGRADED), utcnow())


def test_apply_and_drift(dev: Environment, app: str) -> None:
    manifest = deployment(app, replicas=2)
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": app, "labels": {support.NAME_LABEL: app}},
        "spec": {"selector": {support.NAME_LABEL: app}, "ports": [{"port": 80}]},
    }

    applied = dev.apply([manifest])

    assert "--server-side" in applied.cmd
    assert dev.drift([manifest]).changed == ()
    assert dev.drift([deployment(app, replicas=3)]).changed == (f"Deployment/{app}",)
    assert dev.drift([manifest, service]).changed == (f"Service/{app}",)


def test_drift_ignores_controller_updates(dev: Environment, app: str) -> None:
    """Right after creation the Deployment controller writes status and the revision annotation."""
    for i in range(20):
        name = f"{app}-{i}"
        manifest = deployment(name, replicas=2)
        try:
            dev.apply([manifest])
            assert dev.drift([manifest]).changed == ()
        finally:
            dev.delete("deployment", name)


def test_dry_run_rejects_invalid(dev: Environment, app: str) -> None:
    bad = support.release(app, "1.0.0", image(app), name=f"{app}-9.9.9")

    with pytest.raises(Invalid, match=re.escape("metadata.name must be <app>-<version>")):
        dev.apply([bad], dry_run=True)


def test_patch_status(dev: Environment, app: str) -> None:
    name = f"{app}-1.0.0"
    dev.apply([support.release(app, "1.0.0", image(app))])

    dev.patch_status("releases.deploysim.io", name, {"status": {"phase": "Deploying"}})

    status = dev.get("releases.deploysim.io", name).json()["status"]
    assert status == {"phase": "Deploying"}
