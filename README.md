# deploysim

A deployment controller that promotes a versioned container image through `dev → staging → prod`
on a local [KWOK](https://kwok.sigs.k8s.io/) cluster. Each deployment runs
`VALIDATE → PREPARE → DEPLOY → VERIFY → COMPLETE`, and the controller rolls back automatically to the
last known-good release when verification fails.

KWOK runs a real kube-apiserver, etcd, kube-controller-manager and kube-scheduler against fake nodes.
Deployments, ReplicaSets, rollouts and progress deadlines behave as they do in a real cluster, but no
container ever runs. Pod readiness, and the failures used to exercise rollback, are scripted with KWOK
Stages. Manifests are validated offline with [kubectl-validate](https://github.com/kubernetes-sigs/kubectl-validate)
before anything touches the cluster.

## How this meets the brief

| Requirement | Where |
|---|---|
| Validate the artifact/manifest before making a change | [Validation](#validation) |
| Record the version and target environment being changed | [In-cluster records](#in-cluster-records), `PREPARE` stage |
| Simulated post-deployment health check | [How a deployment runs](#how-a-deployment-runs), [Fault injection](#fault-injection) |
| Stop promotion on failed validation or health check | [How a deployment runs](#how-a-deployment-runs), [promotion gate](#validation) |
| Automatic rollback to the previous known-good version | [How a deployment runs](#how-a-deployment-runs) |
| Safe to retry, never ambiguous | [Retry and recovery](#retry-and-recovery) |
| Audit-friendly result | [Outputs](#outputs) |
| Tests: success, invalid input, failed health check, repeated execution | [Tests](#tests) |
| CI build/test/package workflow | [CI](#ci) |
| Production controls | [Production controls](#production-controls) |
| Assumptions | [Assumptions](#assumptions) |
| Time spent, and what another day would add | [Time spent](#time-spent) |
| AI-assisted development process | [AI_PROCESS.md](AI_PROCESS.md) |

## Quick start

Requires Python 3.11+ on macOS or Linux, and `jq` for the demo. Docker is not needed: KWOK's binary
runtime runs the control plane as local processes.

Without a cluster, in about a minute: pinned tools, a virtualenv, and the unit tests.

```sh
make tools install-dev test-unit
```

The full run, against a local cluster:

```sh
make tools          # pinned kwokctl, kwok, kubectl, kubectl-validate into ./bin, checksums verified
make cluster-up     # KWOK cluster: fake nodes, dev/staging/prod namespaces, Release CRD, fault stages
make install-dev    # venv with deploysim, pytest, ruff, mypy
make test           # unit and end-to-end tests against the cluster, 85% coverage gate (about 4 min)
make demo           # 23 scenarios end to end, asserting every exit code (about 80 s)
make cluster-down
```

`make demo` runs on copies of the example bundles under a fresh app name (`demo-<timestamp>`), with
shorter deadlines so failures take seconds. It deletes that app afterwards, unless `KEEP=1`, and
never touches your own deployments. Its output shows each scenario's exit code and outcome:

```
Automatic rollback
  ok    exit 2   ROLLED_BACK          1.1.0 to staging, unhealthy: back to 1.0.0
  ok    exit 2   ROLLED_BACK          1.2.0 (broken build) to dev: back to 1.1.0
Crash recovery
  ok    exit 70                       1.1.0 to staging crashes after DEPLOY
  ok    exit 4   LOCKED               deploy while the dead run's lease is live
  ok    exit 0   SUCCEEDED            deploy after expiry: recovers, then succeeds
...
all 23 scenarios exited as expected
```

`make cluster-up` writes its kubeconfig to `.deploysim/kubeconfig`. It never reads or modifies
`~/.kube/config` or changes your current context.

A manual walkthrough. `make install-dev` installs `deploysim` into `.venv`, so activate it first:

```sh
source .venv/bin/activate
deploysim validate examples/orders-api-1.0.0.yaml
deploysim deploy   examples/orders-api-1.0.0.yaml --env dev
deploysim deploy   examples/orders-api-1.0.0.yaml --env staging
deploysim deploy   examples/orders-api-1.1.0.yaml --env dev
deploysim deploy   examples/orders-api-1.1.0.yaml --env staging --inject-fault unhealthy   # rolls back to 1.0.0
deploysim deploy   examples/orders-api-1.2.0.yaml --env dev     # a broken build: rolls back to 1.1.0
deploysim status   --app orders-api --env staging
deploysim history  --app orders-api --verify

bin/kubectl --kubeconfig .deploysim/kubeconfig get releases -A
```

## The simulated environment

`make cluster-up` (`scripts/cluster-up.sh`) creates one KWOK cluster with:

| Piece | Purpose |
|---|---|
| Kubernetes control plane at a pinned version | The same version the validation schemas come from |
| Three fake nodes | Somewhere for the scheduler to place pods |
| Namespaces `dev`, `staging`, `prod`, labelled `deploysim.io/simulator=true` | One namespace per environment |
| `Release` CRD (`crds/release.yaml`) | The release record, with server-side immutability |
| KWOK Stages (`kwok/stages.yaml`) | Pod lifecycle, including the [fault](#fault-injection) stages |

`make cluster-up` is safe to re-run:
- A running cluster is reconciled in place.
- A stopped cluster, for example after a reboot, is recreated empty.
- A cluster created from different stages or versions is refused, with instructions to recreate it.

Every run ends by checking that a canary pod becomes Ready.

**Safety guard.** deploysim only talks to the cluster in `--kubeconfig` (default
`.deploysim/kubeconfig`). Every kubectl call passes that file as `--kubeconfig` and as
`$KUBECONFIG`, so `~/.kube/config` is never read. Before any cluster call, deploysim refuses to
continue, with exit code 5, unless both of these hold:
- the current context's API server is on loopback (`127.0.0.1`, `::1` or `localhost`);
- the target namespace exists and carries the `deploysim.io/simulator=true` label.

`deploysim/kube.py` is the only module that runs kubectl, and a unit test enforces that.

## Release bundle

A release is one multi-document YAML file. It holds exactly one `Release`, plus the workload objects
to apply. The bundle carries no namespace: the same file is promoted to every environment with
`--env`.

```yaml
apiVersion: deploysim.io/v1alpha1
kind: Release
metadata:
  name: orders-api-1.1.0
spec:
  app: orders-api
  version: 1.1.0
  image: registry.example.com/orders-api@sha256:4f9c2e7d1b0a8e6f3c5d2b1a09e8f7c6d5b4a3928170f6e5d4c3b2a1908f7e6d
  healthCheck:
    timeoutSeconds: 60
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
  labels:
    app.kubernetes.io/name: orders-api
spec:
  replicas: 3
  progressDeadlineSeconds: 45
  selector:
    matchLabels:
      app.kubernetes.io/name: orders-api
  template:
    metadata:
      labels:
        app.kubernetes.io/name: orders-api
        app.kubernetes.io/version: 1.1.0
    spec:
      containers:
      - name: orders-api
        image: registry.example.com/orders-api@sha256:4f9c2e7d1b0a8e6f3c5d2b1a09e8f7c6d5b4a3928170f6e5d4c3b2a1908f7e6d
        readinessProbe:
          httpGet: { path: /ready, port: 8080 }
---
apiVersion: v1
kind: Service
metadata:
  name: orders-api
  labels:
    app.kubernetes.io/name: orders-api
spec:
  selector:
    app.kubernetes.io/name: orders-api
  ports:
  - port: 80
    targetPort: 8080
```

## Validation

Validation runs in two passes. Neither pass writes anything to the cluster, and any failure ends the
run as `REJECTED` (exit 1).

### Pass 1: offline, no lock, no cluster access

This pass also runs on its own as `deploysim validate`, which is how CI checks a release on a pull
request. The layers run in order, and the first one that fails ends validation, so every violation
reported belongs to one layer. Each violation is reported as `<source>:<code>`.

| Layer | Tool | Catches | Reported as |
|---|---|---|---|
| File | deploysim | Unreadable file | `file:READ` |
| YAML | PyYAML | Unparseable YAML | `yaml:PARSE` |
| Structure | deploysim | No documents; a document that is not a map; anything but exactly one `Release`; no `Deployment`; kinds other than apps/v1 `Deployment`, v1 `Service`, v1 `ConfigMap` | `bundle:EMPTY`, `bundle:NOT_AN_OBJECT`, `bundle:ONE_RELEASE`, `bundle:HAS_DEPLOYMENT`, `bundle:KIND_ALLOWED` |
| Schema | `kubectl-validate` with `--local-schemas schemas/ --local-crds crds/` and `KUBECONFIG=/dev/null` | Unknown fields (`replica:` for `replicas:`), wrong types, missing required fields | `kubectl-validate:<field>`, e.g. `kubectl-validate:spec.replica` |
| Release rules | `kubectl-validate`, using the CRD's OpenAPI schema and CEL rules | `version` not semver; `image` not pinned by `@sha256:` digest; `healthCheck.timeoutSeconds` outside 5–600; `metadata.name` not `<app>-<version>` | `kubectl-validate:spec.version`, `kubectl-validate:spec.image`, `kubectl-validate:spec.healthCheck.timeoutSeconds`, and `kubectl-validate:(root)` for the name rule |
| Bundle policy | deploysim | Rules that span documents, listed below | `bundle:<RULE>` |

Bundle policy, the rules a per-document schema cannot express:

| Rule | Requires |
|---|---|
| `NO_NAMESPACE` | No object sets `metadata.namespace`. deploysim assigns the environment's namespace. |
| `NAME_LABEL` | Every workload object is labelled `app.kubernetes.io/name: <app>`. |
| `VERSION_LABEL` | Every Deployment's pod template is labelled `app.kubernetes.io/version: <version>`. |
| `IMAGE_DIGEST` | Every container and init container image is pinned by `@sha256:` digest. |
| `IMAGE_MATCHES_RELEASE` | Every Deployment has a container named `<app>`, running exactly `Release.spec.image`. |
| `READINESS_PROBE` | Every container has a `readinessProbe`. Without one, "Ready" says nothing about health. |
| `PROGRESS_DEADLINE` | `progressDeadlineSeconds` ≤ `healthCheck.timeoutSeconds`, so Kubernetes declares a stuck rollout failed before deploysim stops waiting. Unset counts as Kubernetes' default of 600. |

Each rule has an invalid example in `tests/fixtures/invalid/`, whose header names the violation it
must produce. kubectl-validate takes about 0.1 s per bundle.

```
$ deploysim validate tests/fixtures/invalid/unknown-field.yaml
REJECTED Deployment/orders-api: kubectl-validate:spec.replica: Invalid value: value provided for unknown field
{
  "file": "tests/fixtures/invalid/unknown-field.yaml",
  "result": "REJECTED",
  "release": null,
  "bundle_digest": null,
  "violations": [
    {
      "source": "kubectl-validate",
      "code": "spec.replica",
      "document": "Deployment/orders-api",
      "message": "Invalid value: value provided for unknown field"
    }
  ]
}
```

The JSON goes to stdout; the `REJECTED` lines go to stderr. Exit codes:
- 0 for `VALID`;
- 1 for `REJECTED`;
- 5 when kubectl-validate, `schemas/` or `crds/` is missing, or the audit log cannot be written.

Every run appends one `VALIDATE` record to the audit log.

Two details matter here:
- **Schema parity.** `schemas/` is exported from the KWOK cluster's own OpenAPI endpoint
  (`make schemas`), so offline validation matches the Kubernetes version being deployed to.
  kubectl-validate's latest release bundles schemas for older Kubernetes versions only, and it fetches
  any other version from GitHub at run time. Exported schemas avoid both problems. CI checks that
  `schemas/` still matches the cluster.
- **Isolation.** Without `KUBECONFIG=/dev/null`, kubectl-validate contacts your current kube context
  to look up schemas. That includes running its credential plugin, such as `aws eks get-token`.
  deploysim always runs it isolated, so validation cannot reach a real cluster.
  `test_isolated_from_kubeconfig` proves this with a kubeconfig whose plugin leaves a marker file.

### Pass 2: against the target environment, while holding the lock

| Check | Catches | Reported as |
|---|---|---|
| Environment | `--env` is not one of `dev`, `staging`, `prod` (the CLI refuses it as a usage error), or the namespace fails the safety guard (exit 5) | `environment:UNKNOWN` |
| Degraded environment | An environment whose last rollback failed rejects deploys unless `--allow-degraded` is passed | `environment:DEGRADED` |
| Promotion gate | `staging` and `prod` only accept a Release whose spec and bundle digest are identical to one that `Succeeded` in the previous environment | `promotion:GATE` |
| Version immutability | A version already deployed in this environment with a different Release spec, or with different manifests (bundle digest). The API server enforces both too (see below). | `immutability:RELEASE_SPEC`, `immutability:BUNDLE` |
| Server dry run | `kubectl apply --server-side --dry-run=server` of the Release, the bundle ConfigMap and every workload object. Catches admission errors and immutable fields such as a changed Deployment selector. | `server:DRY_RUN` |

The degraded, promotion and immutability checks all run before the dry run, and every check that
fails is reported. A rejected run changes nothing in the cluster: the Lease is taken and released,
and that's all.

Deploying an older version than the current one is allowed, since it is how an operator rolls back by
hand. The `PREPARE record_intent` audit record marks it `"downgrade": true`. Versions are compared by
semantic-version precedence, so `1.0.0-rc.1` < `1.0.0` < `1.10.0`.

## How a deployment runs

```
 VALIDATE (offline) ──fail──────────────────────────────────────► REJECTED         exit 1
        │
      LOCK  (Lease) ──held by a live run─────────────────────────► LOCKED           exit 4
        │
     RECOVER  only if a previous run died ──restore fails────────► ROLLBACK_FAILED  exit 3
        │
 VALIDATE (cluster) ──fail──────────────────────────────────────► REJECTED         exit 1
        │
  desired state? ──target current, no drift, rollout healthy─────► NO_OP            exit 0
        │
     PREPARE   record intent, store bundle, Release phase=Deploying
        │
     DEPLOY    kubectl apply --server-side
        │
     VERIFY ──ProgressDeadlineExceeded or timeout──► ROLLBACK ──ok───► ROLLED_BACK      exit 2
        │                                               └──fail──► ROLLBACK_FAILED  exit 3
     COMPLETE  last_known_good = target, Release phase=Succeeded
        │
    SUCCEEDED                                                                      exit 0
```

| Stage | What it does |
|---|---|
| `LOCK` | Creates the `Lease` `deploysim-<app>` in the environment namespace. A Lease held by a live run gives `LOCKED`. An expired Lease is taken over with a compare-and-swap write, and the takeover is audited. The Lease is renewed on every rollout poll, in `VERIFY`, `ROLLBACK` and `RECOVER`. |
| `RECOVER` | Runs when the state still has an `in_flight` record but no live run holds the Lease, which means the previous run died. It re-applies that record's `previous` release, deletes the target's objects that release does not have, and verifies it. If there was no previous release, it deletes the target's stored objects and the environment becomes `EMPTY`. It then clears `in_flight`, records the attempt as `INTERRUPTED`, and sets the target Release's phase to `Interrupted`. If the restored release fails its check, or its stored bundle ConfigMap is missing, the environment becomes `DEGRADED`, the outcome is `ROLLBACK_FAILED` (exit 3), and the requested deploy does not run. |
| `PREPARE` | Writes `in_flight` (deployment ID, attempt, target, `previous` = `last_known_good`, stage) to the state ConfigMap. Stores the bundle in an immutable ConfigMap. Applies the `Release` and sets its phase to `Deploying`. All of this happens before the workload changes, so a crash at any later point is recoverable. |
| `DEPLOY` | `kubectl apply --server-side --field-manager=deploysim --force-conflicts` of the workload objects, each labelled `app.kubernetes.io/managed-by: deploysim`. |
| `VERIFY` | Polls every Deployment in the bundle every 0.5 s and renews the Lease on each poll. It passes when the rollout is complete, by the same rule `kubectl rollout status` uses: `observedGeneration` is current, no old replicas remain, and the updated and available replica counts equal `replicas`. It fails as soon as the `Progressing` condition reports `ProgressDeadlineExceeded`, or when `healthCheck.timeoutSeconds` elapses. |
| `COMPLETE` | Sets `current` and `last_known_good` to the target, clears `in_flight`, and sets the Release phase to `Succeeded`. |
| `ROLLBACK` | Re-applies the `last_known_good` release's bundle from its immutable ConfigMap, deletes the objects only the failed release had (by kind and name), and verifies it the same way, with that release's own `healthCheck.timeoutSeconds`. If that check passes, the outcome is `ROLLED_BACK` (exit 2) and the Release phase `RolledBack`. If it fails, the outcome is `ROLLBACK_FAILED` (exit 3), the environment is `DEGRADED`, and the phase is `RollbackFailed`. If the known-good bundle ConfigMap is missing, nothing is restored: the outcome is `ROLLBACK_FAILED`, `current` stays the failed release, and the error names the ConfigMap. On a first deployment there is nothing to restore: the bundle's objects are deleted and the environment returns to `EMPTY`. |

Rollback re-applies the known-good bundle rather than running `kubectl rollout undo`. `rollout undo`
only reverts the pod template, never the Services and ConfigMaps alongside it. It also depends on
ReplicaSet history still being retained, and it needs to know what was running, which a crashed run
cannot tell it.

### Fault injection

Faults come from KWOK Stages that match a pod-template annotation, so the controller sees a genuine
`ProgressDeadlineExceeded` from kube-controller-manager rather than a mocked result.

| `deploysim.io/fault` | KWOK behaviour | Effect |
|---|---|---|
| `unhealthy` | Pods run but never become Ready (`CrashLoopBackOff`) | Rollout misses its progress deadline, so the deployment rolls back |
| `slow-start` | Pods become Ready after 5 s | Deployment succeeds after waiting |

The annotation can be set in two places:
- **In the bundle**, to simulate a broken build. `examples/orders-api-1.2.0.yaml` fails in every
  environment.
- **By the CLI**, to simulate an environment-specific failure:

| `--inject-fault` | Effect |
|---|---|
| `unhealthy` | Annotates the target's pod template, so this deployment rolls back |
| `unhealthy-rollback` | Annotates the rollback too, so the environment ends `DEGRADED` |
| `crash-after-prepare` | The process exits (code 70) right after `PREPARE`. The intent record and stored bundle are written; the workload is untouched. |
| `crash-after-deploy` | The process exits (code 70) right after `DEPLOY`. The workload has changed but was never verified. |
| `crash-during-rollback` | Makes the target unhealthy, then exits (code 70) right after the rollback's apply, before it is verified |

Every crash leaves the Lease held and the intent record in place, exactly as a killed process
would. The next `deploy` or `recover` after the Lease expires (`--lease-seconds`) resolves it. An
`inject_crash` audit record marks where the run stopped.

The CLI adds the annotation only to the manifests it applies. The bundle stored for rollback never
carries it, so a later rollback to that release is healthy. The `DEPLOY` and `ROLLBACK` audit
records name any fault injected.

## In-cluster records

All state lives in the cluster, in the environment's namespace, so any runner sees the same truth.

| Object | Name | Holds |
|---|---|---|
| `Release` (deploysim.io/v1alpha1) | `<app>-<version>` | What was asked for, annotated `deploysim.io/bundle-digest`, and its status: phase (`Deploying`, `Verifying`, `Succeeded`, `RolledBack`, `RollbackFailed`, `Interrupted`), attempts, deployment ID, message, last transition |
| `ConfigMap`, immutable | `deploysim-<app>-<version>` | The workload objects under `objects.json`, annotated with the bundle digest, used for rollback |
| `ConfigMap` | `deploysim-<app>` | Controller state: `current`, `last_known_good`, `in_flight`, `attempts`, `status` |
| `Lease` | `deploysim-<app>` | The lock: holder, duration, renew time |

After the walkthrough above, on a fresh cluster:

```
$ bin/kubectl --kubeconfig .deploysim/kubeconfig get releases -n staging
NAME               VERSION   PHASE        ATTEMPTS   AGE
orders-api-1.0.0   1.0.0     Succeeded    1          51s
orders-api-1.1.0   1.1.0     RolledBack   1          47s
```

The state ConfigMap's `state.json` at that point:

```json
{
  "app": "orders-api",
  "environment": "staging",
  "status": "HEALTHY",
  "current": {
    "release": "orders-api-1.0.0",
    "version": "1.0.0",
    "image": "registry.example.com/orders-api@sha256:60721df325892970d02392ff6de5b353f71146cd60b3707184b8d2ec58ba449b"
  },
  "last_known_good": {
    "release": "orders-api-1.0.0",
    "version": "1.0.0",
    "image": "registry.example.com/orders-api@sha256:60721df325892970d02392ff6de5b353f71146cd60b3707184b8d2ec58ba449b"
  },
  "in_flight": null,
  "last_outcome": {
    "deployment_id": "dep-ccff7c080b29",
    "attempt": 1,
    "outcome": "ROLLED_BACK"
  },
  "attempts": {
    "dep-9037d87078d5": 1,
    "dep-ccff7c080b29": 1
  },
  "updated_at": "2026-10-26T12:13:19.843Z"
}
```

`status` is one of `EMPTY`, `HEALTHY`, `DEPLOYING` (an `in_flight` record exists), or `DEGRADED`
(a rollback failed).

Every state write is a `kubectl replace` that carries the `resourceVersion` it read; the very first
is a `kubectl create`. A concurrent writer gets `409 Conflict`, or `AlreadyExists` on that first
write, instead of silently overwriting. This compare-and-swap is also the fencing
token: a run whose Lease expired and was taken over cannot clobber the new holder's state.

Version immutability is enforced in two places by the API server, not just by deploysim. A CEL
transition rule on `Release.spec` (`self == oldSelf`) rejects re-using a version for a different
image. The immutable bundle ConfigMap rejects re-using a version for different manifests.

## Commands

```
deploysim validate RELEASE_FILE
deploysim deploy   RELEASE_FILE --env ENV [--inject-fault FAULT] [--allow-degraded] [--lease-seconds N]
deploysim recover  --app APP --env ENV [--lease-seconds N]
deploysim status   --app APP --env ENV
deploysim history  [--app APP] [--env ENV] [--deployment-id ID] [--verify]
```

These options apply to every command:
- `--kubeconfig` (default `.deploysim/kubeconfig`)
- `--audit-log` (default `.deploysim/audit.jsonl`)

Relative defaults, along with `bin/`, `schemas/` and `crds/`, resolve from the project root. The
project root is the current directory, or `$DEPLOYSIM_ROOT` when set. Outside a checkout, the
installed wheel uses its packaged copies of `schemas/` and `crds/`, and `kubectl` and
`kubectl-validate` from `PATH`; pass `--kubeconfig` to reach the simulator.

What each command does:
- `validate` runs pass 1 only. It needs no cluster.
- `deploy` runs the full pipeline. It prints a JSON summary on stdout and one progress line per
  audit record on stderr, for example `deploysim: VERIFY    rollout_status   OK`.
- `recover` resolves an interrupted deployment without starting a new one. It ends `RECOVERED`
  (exit 0), or `NO_OP` (exit 0) when there is nothing to recover, or `ROLLBACK_FAILED` (exit 3)
  when the restored release is unhealthy. A live Lease gives `LOCKED` (exit 4).
- `status` prints, as JSON, the state ConfigMap, every Release of the app in that environment with
  its phase, and the live rollout status of its Deployments.
- `history` prints the matching audit records as JSON lines, filtered by `--app`, `--env` and
  `--deployment-id`. `--verify` checks the whole log's hash chain and sequence numbers. It prints
  `{"records", "verified", "problems"}` and exits 1 if the log was altered, naming each altered
  line on stderr.

A usage error (a missing or unknown argument, an `--env` outside `dev`, `staging` and `prod`, or no
command at all) exits 64.

## Outputs

### Deployment summary (stdout)

The walkthrough's fifth command, which rolls 1.1.0 back in staging. This is real output; only
the `detail` of steps other than `rollout_status` is left out here.

```json
{
  "deployment_id": "dep-ccff7c080b29",
  "attempt": 1,
  "app": "orders-api",
  "environment": "staging",
  "from": "orders-api-1.0.0",
  "to": "orders-api-1.1.0",
  "outcome": "ROLLED_BACK",
  "recovery": { "performed": false, "interrupted": null, "to": null, "result": null },
  "rollback": { "performed": true, "to": "orders-api-1.0.0", "result": "SUCCEEDED" },
  "drift_corrected": false,
  "final": "orders-api-1.0.0",
  "environment_status": "HEALTHY",
  "started_at": "2026-10-26T12:12:32.322Z",
  "finished_at": "2026-10-26T12:13:20.009Z",
  "violations": [],
  "steps": [
    { "seq": 54, "stage": "VALIDATE", "action": "validate_bundle", "result": "OK" },
    { "seq": 55, "stage": "LOCK", "action": "acquire_lease", "result": "OK" },
    { "seq": 56, "stage": "VALIDATE", "action": "load_state", "result": "OK" },
    { "seq": 57, "stage": "VALIDATE", "action": "promotion_gate", "result": "OK" },
    { "seq": 58, "stage": "VALIDATE", "action": "immutability", "result": "OK" },
    { "seq": 59, "stage": "VALIDATE", "action": "server_dry_run", "result": "OK" },
    { "seq": 60, "stage": "VALIDATE", "action": "desired_state", "result": "DEPLOY" },
    { "seq": 61, "stage": "PREPARE", "action": "record_intent", "result": "OK" },
    { "seq": 62, "stage": "PREPARE", "action": "store_bundle", "result": "OK" },
    { "seq": 63, "stage": "PREPARE", "action": "record_release", "result": "OK" },
    { "seq": 64, "stage": "PREPARE", "action": "release_phase", "result": "OK" },
    { "seq": 65, "stage": "DEPLOY", "action": "apply", "result": "OK" },
    { "seq": 66, "stage": "DEPLOY", "action": "record_stage", "result": "OK" },
    { "seq": 67, "stage": "VERIFY", "action": "release_phase", "result": "OK" },
    { "seq": 68, "stage": "VERIFY", "action": "rollout_status", "result": "FAIL",
      "detail": {"reason": "ProgressDeadlineExceeded", "waited_s": 46.2, "polls": 75, "deployments": [{"name": "orders-api", "message": "progress deadline exceeded: 1 of 3 replicas updated, 3 available"}]} },
    { "seq": 69, "stage": "ROLLBACK", "action": "record_stage", "result": "OK" },
    { "seq": 70, "stage": "ROLLBACK", "action": "load_bundle", "result": "OK" },
    { "seq": 71, "stage": "ROLLBACK", "action": "apply", "result": "OK" },
    { "seq": 72, "stage": "ROLLBACK", "action": "rollout_status", "result": "OK",
      "detail": {"reason": "Complete", "waited_s": 0.1, "polls": 1, "deployments": [{"name": "orders-api", "message": "3/3 available"}]} },
    { "seq": 73, "stage": "ROLLBACK", "action": "record_outcome", "result": "OK" },
    { "seq": 74, "stage": "ROLLBACK", "action": "release_phase", "result": "OK" },
    { "seq": 75, "stage": "LOCK", "action": "release_lease", "result": "OK" }
  ],
  "error": "health check failed: ProgressDeadlineExceeded; rolled back to orders-api-1.0.0"
}
```

`deployment_id` is derived from `sha256(app | environment | release spec | bundle digest)`, so the
same release to the same environment always gets the same ID, and its attempts can be counted.
`attempt` is assigned at `PREPARE`. It is `null` when no deployment was attempted: `REJECTED`,
`LOCKED` and `NO_OP`. `error` is present only when the run did not succeed; a `REJECTED` run
lists its `violations` instead.

### Audit log

`.deploysim/audit.jsonl` holds one record per action, for every invocation, including rejected ones.
Each record is flushed and fsynced before the next action starts.

The walkthrough's `DEPLOY apply` record for that run, formatted here (one line in the file;
`cmd` holds absolute paths, shortened here):

```json
{
  "seq": 65,
  "ts": "2026-10-26T12:12:33.192Z",
  "deployment_id": "dep-ccff7c080b29",
  "attempt": 1,
  "app": "orders-api",
  "environment": "staging",
  "stage": "DEPLOY",
  "action": "apply",
  "result": "OK",
  "cmd": [
    "bin/kubectl",
    "--kubeconfig",
    ".deploysim/kubeconfig",
    "apply",
    "--server-side",
    "--field-manager=deploysim",
    "--force-conflicts",
    "-n",
    "staging",
    "-f",
    "-",
    "-o",
    "json"
  ],
  "detail": {
    "objects": [
      "ConfigMap/orders-api-config",
      "Deployment/orders-api",
      "Service/orders-api"
    ],
    "injected_fault": "unhealthy"
  },
  "prev_hash": "3323d79803ea6864028c00557c9d4b9e685cbc5c718d739115cdcafd6845a3e1",
  "hash": "6a4e177f407b8619b82e5fa26c0e9ee2e0fded862f9dabfb83e25dd8c1f13475"
}
```

- `seq` is gap-free and is the ordering authority; `ts` is wall-clock UTC for humans.
- `cmd` is the exact command run, so an operator can replay or inspect any step by hand.
- `hash` is `sha256(prev_hash + record)`, so editing or deleting any line breaks `history --verify`.
- Appends take an exclusive file lock, so concurrent runs for different apps never interleave records.
- If the last line is incomplete, for example after a power loss mid-write, appends stop with an
  error instead of chaining onto it. `history --verify` shows the damaged line.

### Exit codes

| Code | Outcome |
|---|---|
| 0 | `SUCCEEDED`, `NO_OP` or `RECOVERED` |
| 1 | `REJECTED`: validation failed, nothing changed |
| 2 | `ROLLED_BACK`: health check failed, previous release restored and healthy |
| 3 | `ROLLBACK_FAILED`: environment `DEGRADED`, operator action needed |
| 4 | `LOCKED`: another live run holds the Lease |
| 5 | `ENVIRONMENT_ERROR`: missing tool, unreachable cluster, a kubectl or kubectl-validate timeout, unwritable audit log, safety guard refused, the Lease was taken over, or an unexpected internal error (with a traceback on stderr). `deploy` and `recover` still print their summary and release the Lease. If it happens mid-deployment, the next run recovers. |
| 64 | Usage error (`EX_USAGE`, so it never collides with 2) |
| 70 | Simulated crash (`--inject-fault crash-*`). Nothing is cleaned up; the next run recovers. |

## Retry and recovery

Deploys converge on a desired state: "this release is current, matches its bundle, and is rolled
out". Re-running a command is always safe.

| Situation when `deploy` runs | Result |
|---|---|
| Target is current, applying its bundle would change nothing (see below), and the rollout is healthy | `NO_OP`, exit 0. Nothing is written except the Lease; the `desired_state` audit record shows the check. |
| Target is current but has drifted or is unhealthy | Re-applied and verified as the next attempt. The summary marks `"drift_corrected": true`, and `desired_state` lists the drifted objects. |
| A different release is current | Normal deployment |
| This release was previously rolled back | Deploys again as attempt N+1. Transient failures are common, and the attempt count shows in the Release status and the audit log. |
| A previous run died (`in_flight` present, Lease expired) | `RECOVER` first, then deploy as attempt N+1 |
| A previous run is still alive (Lease held and renewed) | `LOCKED`, exit 4 |
| Environment is `DEGRADED` | `REJECTED` unless `--allow-degraded`, so fixing forward is a deliberate human decision |

Recovery rolls back to the intent record's `previous` release and verifies it, whatever stage the dead
run had reached. If the restored release fails its health check, or its stored bundle ConfigMap has
been deleted, the environment becomes `DEGRADED` and the requested deployment does not run. To get
out of `DEGRADED`, redeploy that version, or a fixed one, with `--allow-degraded`.

At no point can the environment be in a state deploysim cannot name. The intent record is written
before the workload changes. Every state write is a compare-and-swap. Every non-terminal state has one
defined recovery path. Any error, expected or not, ends with exit 5 and never with exit 1, which
always means "rejected, nothing changed".

A crashed run holds its Lease until it expires (`--lease-seconds`, default 30), so the next run reports
`LOCKED` until then. That is the price of telling a dead run from a slow one without asking it.

**Drift detection.** "Would applying this bundle change anything?" is answered by comparing each
live object with a server-side dry-run apply. The comparison ignores `.status` and
`managedFields`, and is made only when the two reads have the same `resourceVersion`.
- When a controller wrote in between, the check is retried, up to 5 times.
- An object that keeps changing counts as drift. Re-applying it is harmless.
- Only fields the bundle sets count. Changing one, such as `replicas` or ConfigMap `data`, is drift.
  A field someone else added, such as an extra label, is not, because server-side apply leaves it
  alone.

Plain `kubectl diff` can't be used for this. Straight after an apply, it reported changes in 2 of
15 runs: status updates, and the `deployment.kubernetes.io/revision` annotation that the Deployment
controller adds a moment after creation.

## Design decisions

- **State lives in the cluster, not on the runner.** CI jobs are ephemeral. A new runner, or an
  operator's laptop, must see the same current and known-good releases.
- **Lease for exclusion, compare-and-swap for correctness.** The Lease stops two deploys of one app
  to one environment. The `resourceVersion` check stops a run that lost its Lease from writing state.
- **Validate offline first.** Most mistakes are caught with no cluster access, no lock, and in
  well under a second. The same check gates pull requests.
- **Failures come from the simulated cluster, not from mocks.** End-to-end tests drive the same code
  path a real stuck rollout would.
- **Crash recovery rolls back, never forward.** After a crash, the target's health is unknown and the
  previous release's is known. Retrying is the next command's job, under a new attempt number.
- **A failed rollback stops; it never loops.** `ROLLBACK_FAILED` exits with its own code so it can page
  a human, and the environment stays `DEGRADED` until someone overrides it.
- **kubectl, not a client library.** Every cluster action is a command an operator could run, and the
  audit log records it verbatim. The only Python dependency is PyYAML.

## Tests

```sh
make test-unit   # no cluster needed
make test-e2e    # needs `make cluster-up`
make test        # both in one run, failing under 85% coverage of deploysim/
```

Coverage includes the `deploysim` CLI processes that the end-to-end tests start (coverage's
`patch = ["subprocess"]`), so it measures the controller as it actually runs.

Unit tests take about 6 seconds, most of it kubectl-validate. End-to-end tests each use a unique app
name, so they share one cluster without interfering. They use a short `progressDeadlineSeconds`, so
the failure cases take seconds.

| File | Scenarios | Brief requirement |
|---|---|---|
| `tests/unit/test_smoke.py` | `deploysim --version` and `python -m deploysim --version` | — |
| `tests/unit/test_install_tool.py` | A tool is installed and recorded; a second run downloads nothing; a wrong checksum leaves no binary; tar members are extracted; a platform with no pinned checksum fails | — |
| `tests/unit/test_validate.py` | Every example passes; each of the 23 fixtures in `tests/fixtures/invalid/` produces exactly the violations its header names; kubectl-validate never runs a hostile kubeconfig's credential plugin (with a control showing it would, unisolated); the bundle digest ignores order and formatting; a kubectl-validate timeout is an environment error; `validate` exit codes, JSON and audit record | Invalid input |
| `tests/unit/test_bundle.py` | Semantic-version precedence, using the specification's own ordering example | Repeated execution |
| `tests/unit/test_audit.py` | `seq` is gap-free; the hash chain verifies; edited, deleted, reordered and truncated lines are detected; 8 concurrent writers produce one valid chain; every append is fsynced; an unwritable log raises `AuditError`; record fields match this README | Audit-friendly result |
| `tests/unit/test_kube.py` | The safety guard refuses a remote server, a missing kubeconfig, or an unknown context, before any process runs; every call pins `--kubeconfig` and `$KUBECONFIG`; recorded kubectl errors are classified correctly; a kubectl timeout is `Unreachable`; only `kube.py` and `validate.py` start processes | Never touch a real environment |
| `tests/unit/test_health.py` | Rollout decision for every state (unobserved, updating, terminating, unavailable, complete, deadline exceeded); the VERIFY loop waits, renews on every poll, fails fast on the deadline, and times out; an empty Deployment list never counts as complete | Simulated health check |
| `tests/unit/test_controller.py` | Deployment ID is stable, ignores defaulted fields, and changes with environment or bundle; in-cluster manifests carry labels and the bundle digest; fault injection touches only pod templates; outcome-to-exit-code mapping; an unexpected error exits 5 with a summary and releases the Lease | Audit-friendly result, never ambiguous |
| `tests/unit/test_cli.py` | Usage errors exit 64; the deploy summary has exactly this README's keys, in order and with matching types; `history` filters and `history --verify` (exit 1 on a tampered log); `deploy` and `status` refuse a non-loopback kubeconfig with exit 5; a kubectl timeout during `deploy` exits 5 with a summary and an audit record; an unwritable audit log exits 5 without a traceback | Audit-friendly result, never touch a real environment |
| `tests/unit/test_paths.py` | A checkout's `bin/`, `schemas/` and `crds/` are preferred; outside one, tools come from `PATH` and schemas and CRDs from the installed package | Runnable artifact |
| `tests/unit/test_readme.py` | This README's exit-code table equals `ExitCode`; every outcome, command and `--inject-fault` value is documented | — |
| `tests/unit/test_repo.py` | Every CI action is pinned to a commit SHA; PyYAML is the only runtime dependency; every tool has a pinned SHA-256 for all four platforms; no script reads `~/.kube` or calls kwokctl or kubectl without `--kubeconfig` | Never touch a real environment |
| `tests/e2e/test_environment.py` | Nodes Ready; environment namespaces labelled; the API server enforces the Release name rule, Release spec immutability and the immutable bundle ConfigMap; status subresource and printer columns; each KWOK stage (ready, `unhealthy`, `slow-start`) behaves as documented; `schemas/` matches the cluster. `cluster-up` on its own throwaway cluster: creates, re-runs without changes, recreates a stopped cluster, refuses another fingerprint, and leaves `~/.kube/config` unchanged. | The simulated environment |
| `tests/e2e/test_kube.py` | An unlabelled or missing namespace is refused; a Lease is exclusive, renewable, released and taken over after expiry; a stale holder's renew and release fail with `Conflict`; state round-trips and a stale save fails with `Conflict`; drift sees real changes and never controller updates (20 fresh Deployments) | Safe to retry, never ambiguous |
| `tests/e2e/test_deploy.py` | First deployment (Release, state, bundle ConfigMap, Lease released, labels); upgrade replaces every pod; promotion dev → staging → prod; `slow-start` succeeds after waiting; audit trail in stage order with commands; a failed health check exits 2 and leaves `current` untouched; `status` reports state, Releases and rollout | Successful deployment, failed health check |
| `tests/e2e/test_rejections.py` | An invalid bundle creates nothing; the promotion gate rejects a release new to staging and a changed bundle; reusing a version with a different image or manifests is rejected; a held Lease gives exit 4 and changes nothing; a `DEGRADED` environment needs `--allow-degraded` | Invalid input, repeated execution |
| `tests/e2e/test_rollback.py` | `--inject-fault unhealthy` upgrade rolls back to the previous release, and its pods keep serving: ready old pods never drop below `replicas - maxUnavailable`, sampled throughout; the stored bundle stays unannotated; a broken build (fault in the bundle) rolls back; an unhealthy first deployment removes its objects and returns to `EMPTY`; rollback deletes a Deployment only the failed release added; `unhealthy-rollback` ends `DEGRADED` with exit 3; a deleted known-good bundle ConfigMap ends `ROLLBACK_FAILED` and `DEGRADED`, and redeploying that version with `--allow-degraded` recovers; a `DEGRADED` environment needs `--allow-degraded`. After every scenario there is no `in_flight` record. | Failed health check, rollback |
| `tests/e2e/test_idempotency.py` | Same release twice gives `NO_OP`, with no write step and the state, Release, Deployment generation and ReplicaSets unchanged; a manual `kubectl scale` is detected and corrected; retry after rollback is attempt 2 with the same deployment ID; two concurrent deploys give exactly one exit 0 and one exit 4, with consistent state; a downgrade is flagged; a 2 s Lease stays held through a slower VERIFY, because every poll renews it | Repeated execution |
| `tests/e2e/test_recovery.py` | For each crash point (`after-prepare`, `after-deploy`, `during-rollback`): exit 70 leaves `DEPLOYING` and an intent record naming the stage; a deploy while the Lease is live is `LOCKED`; after expiry, the deploy recovers (records the interrupted ID, restores 1.0.0) and then succeeds as attempt 2. `recover`: `LOCKED` while the Lease is live, then `RECOVERED` to the previous release with the Release `Interrupted`; a crashed first deploy recovers to `EMPTY`; recovery deletes objects only the interrupted release added; a deleted known-good bundle ConfigMap ends `ROLLBACK_FAILED` and `DEGRADED`, and the next `recover` is a `NO_OP`; with nothing to recover it is a `NO_OP` that writes nothing. | Never ambiguous |

Behaviour the tests cannot reach on their own is shown by `make demo`, and by CI's `e2e` job, which
runs the tests and the demo against the built wheel.

## Project layout

```
deploysim/
  __main__.py      CLI entry point (python -m deploysim)
  cli.py           argument parsing, exit codes, output
  paths.py         tool, schema and state paths: the checkout, else PATH and packaged copies
  bundle.py        loading release bundles, structure checks, bundle policy, bundle digest
  validate.py      pass 1: kubectl-validate offline, then bundle policy
  errors.py        problems that mean exit code 5: missing tools, unsafe targets
  clock.py         UTC clock and timestamp formats
  kube.py          the only kubectl caller: safety guard, error classification, apply, drift
  lease.py         Lease acquire, renew, takeover, release; all compare-and-swap
  state.py         state ConfigMap with compare-and-swap
  health.py        rollout verification
  outcomes.py      outcomes and exit codes
  controller.py    deploy and recover: Invocation, Operator, Session
  audit.py         hash-chained JSONL audit log
crds/release.yaml        Release CRD: schema, CEL rules, printer columns
kwok/stages.yaml         node and pod Stages, including fault stages
schemas/                 OpenAPI schemas exported from the pinned cluster version
examples/                release bundles used by the demo and the walkthrough
scripts/                 install-tool.sh, cluster-up.sh, cluster-down.sh, export-schemas.sh, demo.sh
tests/unit/  tests/e2e/  tests/fixtures/
.github/workflows/ci.yml
Makefile                 tool versions and checksums are pinned here
pyproject.toml
```

## CI

### Included workflow (`.github/workflows/ci.yml`)

Runs on every pull request and push to `main`. Every action is pinned to a commit SHA.

```
lint ──────┐
unit ──────┤   (all four start together; e2e waits for package)
manifests ─┤
package ───┴──► e2e (KWOK)
```

| Job | Steps |
|---|---|
| `lint` | `make lint`: `ruff check`, `ruff format --check`, `mypy --strict`, `shellcheck scripts/*.sh`, `actionlint` |
| `unit` | `make tools`, then `pytest tests/unit` on Python 3.11 and 3.14 |
| `manifests` | `kubectl-validate crds/`, then `deploysim validate` on every file in `examples/`; every bundle must pass |
| `package` | `make package`: wheel, sdist, CycloneDX SBOM of the installed wheel, `SHA256SUMS`. On pushes to `main`, a build provenance attestation of the wheel and sdist. Uploaded as the `dist` artifact. |
| `e2e` | Installs the `dist` wheel into a clean venv and moves the checkout's `deploysim/` aside, so every import, including the CLI that the tests and demo start, is the wheel. Then `make cluster-up`, `make schemas` followed by `git diff --exit-code schemas/`, `pytest -m e2e tests/e2e`, and `scripts/demo.sh`. Last, from outside the checkout, the wheel validates and deploys an example with its packaged schemas and CRD and the tools on `PATH`. On failure it uploads `e2e-diagnostics`: `kwokctl logs` for each component, `kubectl get events -A`, the objects as YAML, and every audit log the tests and demo wrote. The cluster is always deleted at the end. |

Three details are deliberate:
- **The e2e job tests the wheel, not the checkout.** A packaging mistake, such as a module left out
  of the wheel, fails CI.
- **The Release CRD is validated twice.** `kubectl-validate crds/` checks it offline, and
  `make cluster-up` applies it to a real API server. When `deploysim validate` loads it with
  `--local-crds` to check Releases, kubectl-validate does not re-check the CRD itself: a schema
  combining `additionalProperties` with `properties`, which the API server rejects, is silently
  accepted there.
- **`make tools` downloads kwokctl, kwok, kubectl and kubectl-validate by exact version and verifies
  each against a SHA-256 pinned in the `Makefile`.** CI and laptops run identical tools; `bin/` and
  `~/.kwok/cache` are cached between runs.

To check that a wheel came from this workflow on `main`, download the `dist` artifact from a run on
`main` and verify it:

```sh
shasum -a 256 -c SHA256SUMS
gh attestation verify deploysim-0.1.0-py3-none-any.whl --repo rpop12345/deploysim
```

The attestation is SLSA provenance v1, naming `.github/workflows/ci.yml` on `refs/heads/main` and
the commit. A modified wheel fails verification.

Failure paths, checked locally:
- an example bundle with an unknown field fails `manifests`;
- `schemas/` out of date with the cluster fails the `git diff` step of `e2e`;
- that invalid CRD fails `kubectl-validate crds/` and the API server's apply.

### What a production pipeline adds

- **Build once, promote the digest.** The image is built and signed once. The release bundle is
  generated from the build output and never edited by hand. The same bundle moves through
  `dev → staging → prod`, which the promotion gate enforces.
- **Deploy jobs per environment.** dev deploys automatically on merge. staging deploys automatically
  after dev has been healthy for a bake period. prod waits for approval (below).
- **The controller's exit code drives the pipeline.** `2` and `3` fail the job and notify the owning
  team; `3` also opens an incident. `4` retries with backoff.
- **Audit records leave the runner.** The audit log is shipped to durable storage when every job ends,
  whatever the outcome.

## Production controls

In production this controller's job is usually split across a GitOps controller (Argo CD, Flux) and a
progressive delivery controller (Argo Rollouts, Flagger). The controls below apply either way. Each
one names where it would plug into deploysim.

### Approvals
- Production deploys need approval from someone other than the change author (GitHub Environment
  required reviewers, or a change-advisory process for high-risk changes).
- The approval is bound to the Release name and image digest. Approving 1.1.0 cannot be reused for a
  rebuilt 1.1.0, and the CEL immutability rule makes that enforceable.
- Hook: pass-2 validation requires an approval record for the `deployment_id` when `--env prod`.

### Change windows
- Each environment has allowed windows and freeze periods (holidays, peak events). Outside a window,
  deploys are rejected.
- An emergency override requires an incident ID, is recorded in the audit log, and notifies the
  on-call channel.
- Hook: pass-2 validation checks the window; `deploy --emergency INC-1234` records the override.

### Progressive delivery
- Canary in steps (5% → 25% → 50% → 100%) with automated analysis between steps, or blue/green for
  services where mixed versions are unsafe.
- Rings across regions or clusters, with bake time between them, so a bad release reaches a fraction
  of users first.
- Feature flags so that deploying code and releasing behaviour are separate, reversible decisions.
- Rollback relies on the previous release working against current data, so schema changes use
  expand/contract migrations.
- Hook: `DEPLOY` + `VERIFY` become a loop over traffic steps (an Argo Rollouts `Rollout` in place of
  the `Deployment`); `ROLLBACK` shifts traffic back instead of re-applying.

### Observability gates
- Verification goes beyond "pods are Ready". The new release is compared against the baseline on error
  rate, p99 latency, saturation and SLO burn rate, plus synthetic checks of critical user journeys.
- Every deploy and rollback is annotated on dashboards so regressions line up with the change.
- Thresholds are explicit and versioned with the service, not tuned by hand during a deploy.
- Hook: `VERIFY` adds metric queries (an Argo Rollouts `AnalysisTemplate`) after the rollout check.

### Access controls
- CI authenticates to clusters with short-lived OIDC credentials. There are no long-lived kubeconfigs
  or tokens.
- The deploy identity has a namespace-scoped Role with only the verbs and kinds a bundle may contain,
  plus `Release`, `Lease` and `ConfigMap`. Humans have read access to production, and break-glass
  access is time-limited and audited.
- The bundle policy above is a fast client-side check, not a control: anyone with cluster access can
  bypass it. The same rules run server-side as a `ValidatingAdmissionPolicy` or Kyverno policy:
  digest-pinned images, required probes, and image signature verification (cosign).
- Branch protection and CODEOWNERS on the service and on its release bundles.

### Audit and compliance
- Audit records are shipped to append-only storage (object storage with object lock, or a SIEM) and
  linked to the change ticket. Kubernetes API audit logging covers changes made outside the controller.
- Retention meets the organisation's compliance requirements.
- Hook: the audit writer gains a remote sink; the local hash chain still detects gaps in transit.

## Assumptions

- **One app per bundle, one local cluster.** `dev`, `staging` and `prod` are namespaces in one KWOK
  cluster, standing in for separate clusters.
- **Ready means healthy.** The health check is the rollout: every replica updated and Ready before
  the progress deadline. Error rates and latency are out of scope; see
  [Observability gates](#observability-gates) for where they plug in.
- **Images are built elsewhere.** A bundle references its image by digest; deploysim never builds
  or pulls it.
- **Promotion is strictly `dev → staging → prod`.** No environment can be skipped.
- **Runners' clocks agree.** Lease expiry compares the holder's `renewTime` with the local clock, so
  skew must stay well under `--lease-seconds`. Compare-and-swap still stops a stale run's state
  writes.
- **Tools are installed separately.** The wheel carries the validation schemas and the Release
  CRD, but not `kubectl` or `kubectl-validate`: it uses the pinned copies in `bin/` when run from a
  checkout, and otherwise whatever is on `PATH`.
- **One operator's machine.** The audit log is a local file; shipping it off-host is a
  [production control](#audit-and-compliance).

## Limitations

- **Nothing runs in the pods.** Readiness comes from KWOK Stages, not from probes. VERIFY proves the
  rollout mechanics, not the application's behaviour.
- **Namespaces stand in for environments.** Production environments would be separate clusters, and
  usually separate cloud accounts.
- **No pruning on forward deploys.** An object removed from a bundle is left in place by the next
  deploy. Rollback and recovery do delete the objects only the failed release had.
- **Control-plane binaries are not checksum-pinned here.** kwokctl downloads kube-apiserver,
  kube-controller-manager, kube-scheduler and etcd for the pinned Kubernetes version into
  `~/.kwok/cache` itself. Only the kwok controller comes from the verified `bin/kwok`.
- **Crash detection waits for Lease expiry.** After a crash, the next run is `LOCKED` for up to
  `--lease-seconds`.
- **The Lease is renewed only while waiting on a rollout.** A run that stalls anywhere else for
  longer than `--lease-seconds` can be taken over. Its next state write then fails the
  compare-and-swap, it stops with exit 5, and the next run recovers.
- **Local audit log.** The hash chain makes tampering detectable, not impossible. Anyone who can
  rewrite the whole file can rebuild the chain; shipping records off-host closes that gap.

## Time spent

About 6 hours over 4 days, against the suggested 2. The 2-hour core is validate → apply → verify →
roll back, in `deploysim/controller.py`. The rest went into testing against a real API server rather
than mocks, and into crash recovery and retry safety.

### With another day

- **Unit-test the controller against an in-memory `Environment`.** Rollback, retry and recovery are
  covered end to end on KWOK, but not without a cluster.
- **Classify kubectl errors by API status,** not by matching stderr text, which can change with a
  kubectl upgrade.
- **Renew the Lease throughout a run,** from a background thread, not only while polling a rollout.
- **Move `--inject-fault` out of the shipped CLI,** behind a test-only entry point, since the crash
  faults call `os._exit`.
