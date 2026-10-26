# Implementation plan

Builds deploysim as specified in [README.md](../README.md). Each phase lands as one commit on `main`,
with `make lint test` green.

## Pinned inputs

| Input | Version | Notes |
|---|---|---|
| KWOK (`kwokctl`, `kwok`) | v0.8.0 | Binary runtime; no Docker |
| Kubernetes (cluster) | v1.34.12 | `KWOK_KUBE_VERSION` |
| `kubectl` | v1.34.12 | Same minor as the cluster |
| `kubectl-validate` | v0.0.4 | Used only with schemas exported from the cluster |
| Python | ≥ 3.11 | Runtime dependency: PyYAML only |

## Rules for every phase

These are checked again at the end of each phase. They have been met for P0 to P9.

- All tests from earlier phases still pass.
- `ruff check`, `ruff format --check`, `mypy --strict` and `shellcheck` are clean.
- Behaviour matches the README. Where the README is wrong, it is corrected in the same commit.
- No step reads or writes `~/.kube/config`.

## Status

| Phase | Status | Commit |
|---|---|---|
| P0 Scaffold and tooling | ✅ Done | `975ca45` |
| P1 Simulated environment | ✅ Done | `d010efa` (PR #1, merged as `4e81ac6`) |
| P2 Audit log | ✅ Done | `a01f40f` (PR #2, merged as `712c753`) |
| P3 Bundle loading and offline validation | ✅ Done | `829f0d2` (PR #3, merged as `6b23ac2`) |
| P4 Kubernetes access layer | ✅ Done | `5794c9c` (PR #4, merged as `87f48ea`) |
| P5 Deploy, happy path | ✅ Done | `8bcc598` (PR #5, merged as `7ee0de2`) |
| P6 Rollback and degraded environments | ✅ Done | `e4a0f66` (PR #6, merged as `bad0cf1`) |
| P7 Idempotency, drift and retries | ✅ Done | `206bdf6` (PR #7, merged as `1661258`) |
| P8 Crash recovery | ✅ Done | `fb15d64` (PR #8, merged as `296a390`) |
| P9 CLI completeness and demo | ✅ Done | `4076f2f` (PR #9, merged as `38034e6`) |
| P10 CI complete | ✅ Done locally; ⏳ GitHub run | not yet committed |
| P11 Documentation pass | ⬜ Not started | |

## Phase overview

```
P0 scaffold ─┬─► P1 cluster ─────► P4 kube access ─┐
             ├─► P2 audit ──────────────────────────┼─► P5 deploy ─► P6 rollback ─► P7 idempotency ─► P8 recovery ─► P9 CLI + demo ─► P10 CI complete ─► P11 docs
             └─► P3 validation ─────────────────────┘
```

P1, P2 and P3 are independent and can be built in any order.

---

## P0: Scaffold and tooling

**Status:** ✅ Done

**Goal:** a repository that installs, lints, tests and fetches pinned tools reproducibly.

**Deliverables**
- `pyproject.toml`: package `deploysim`, `requires-python >= 3.11`, dependency `PyYAML`, extra `dev`
  (pytest, pytest-cov, ruff, mypy, types-PyYAML, build, cyclonedx-bom), console script `deploysim`.
- `deploysim/__init__.py` (`__version__`), `deploysim/__main__.py`, and a `cli.py` stub with
  `--version`.
- `Makefile` targets: `tools`, `install-dev`, `lint`, `fmt`, `test-unit`, `test-e2e`, `test`,
  `package`, `clean`.
- `make tools` downloads `kwokctl`, `kwok`, `kubectl` and `kubectl-validate` for darwin/linux on
  amd64/arm64 into `bin/`. Each download is verified against a SHA-256 pinned in the `Makefile`, and a
  tool already present at the right version is skipped.
- `pytest.ini` options in `pyproject.toml`: an `e2e` marker, and `tests/unit` as the default path.
- `.github/workflows/ci.yml` with `lint` and `unit` jobs only.

**Definition of done**
- [x] `make tools` on a clean checkout produces all four binaries; a second run downloads nothing.
- [x] Changing one pinned checksum makes `make tools` fail and delete the downloaded file.
- [x] `make install-dev lint test-unit` passes.
- [x] `deploysim --version` and `python -m deploysim --version` print the package version, with `.venv`
      activated.
- [x] The CI `lint` and `unit` jobs are green on a pushed commit.

**Tests that must pass**

| Test | Asserts | Status |
|---|---|---|
| `tests/unit/test_smoke.py::test_version[module]` and `[console-script]` | Both entry points exit 0 and print `deploysim <__version__>` | ✅ |
| `tests/unit/test_install_tool.py::test_installs_and_stamps` | Binary installed, executable, marker file holds the SHA-256 | ✅ |
| `tests/unit/test_install_tool.py::test_second_run_downloads_nothing` | Second run with an unreachable URL succeeds silently | ✅ |
| `tests/unit/test_install_tool.py::test_checksum_mismatch_leaves_no_binary` | Exit 1; neither binary nor marker file remains | ✅ |
| `tests/unit/test_install_tool.py::test_extracts_tar_member` | The named member is extracted from a `.tar.gz` | ✅ |
| `tests/unit/test_install_tool.py::test_missing_checksum_fails` | No pinned checksum gives exit 1 and nothing is downloaded | ✅ |
| `make tools` run twice | Second run makes no network requests | ✅ 0.1 s, no output |
| `make tools` with a corrupted checksum | Exits non-zero; no binary left in `bin/` | ✅ |
| CI `lint` and `unit` jobs | Green on GitHub | ✅ `975ca45` |

---

## P1: Simulated environment

**Status:** ✅ Done

**Goal:** `make cluster-up` produces the environment the README describes, and the KWOK Stages
behave as the fault model requires.

**Deliverables**
- `crds/release.yaml`:
  - schema with patterns for `app`, `version` and digest-pinned `image`;
  - `healthCheck.timeoutSeconds` from 5 to 600, default 60;
  - CEL rule `metadata.name == app + '-' + version`;
  - CEL transition rule `spec == oldSelf` (spec is immutable);
  - a `status` subresource with a defined `status` schema;
  - printer columns `Version`, `Phase`, `Attempts` and `Age`.
- `kwok/stages.yaml`, holding the complete stage set, because custom stages replace KWOK's defaults:
  - `node-initialize` and `node-heartbeat-with-lease`;
  - `pod-ready`, which skips pods carrying `deploysim.io/fault`;
  - `pod-slow-start`, which becomes Ready after 5 s;
  - `pod-unhealthy`, which runs but never becomes Ready;
  - `pod-delete`.
- `scripts/cluster-up.sh`, which runs in this order:
  1. Create the cluster `deploysim` (binary runtime, kwok controller from `bin/kwok`) with its
     kubeconfig at `.deploysim/kubeconfig`.
  2. Create 3 untainted nodes (`kwok/cluster.yaml`).
  3. Create namespaces `dev`, `staging` and `prod`, labelled `deploysim.io/simulator=true`.
  4. Apply the CRD and wait for it to be established.
  5. Check that a canary pod becomes Ready within 30 s.

  When the cluster already exists, step 1 depends on its state:
  - **Running:** reconcile only.
  - **Stopped:** recreate it empty, because a restarted KWOK v0.8.0 kwok-controller intermittently
    stops managing pods.
  - **Created from different stages or versions:** refuse. This is checked against
    `.deploysim/cluster.fingerprint`.
- `scripts/cluster-down.sh`.
- `scripts/export-schemas.sh` and `make schemas`, writing `schemas/api/v1.json` and
  `schemas/apis/apps/v1.json`, committed to the repo.
- `tests/e2e/conftest.py`:
  - fails fast with a clear message if the cluster is not up;
  - an `app` fixture yielding a unique name (`t-<6 hex>`) and deleting everything carrying that app's
    label on teardown;
  - a `kubectl` fixture bound to `.deploysim/kubeconfig`.

**Definition of done**
- [x] `make cluster-up` from nothing finishes in under 60 s with binaries cached; a second run changes
      nothing. Measured at 4.9 s fresh and 1.1 s on re-run; namespace and CRD resourceVersions unchanged.
- [x] The SHA-256 of `~/.kube/config` is identical before and after `cluster-up` and `cluster-down`.
- [x] `make cluster-down` removes the cluster and `.deploysim/kubeconfig`; with no cluster, it says so
      and exits 0.
- [x] A stopped cluster is recreated by `make cluster-up` (3 of 3 stop and up cycles passed).
- [x] With no cluster, `make test-e2e` exits 1 with "run make cluster-up".
- [x] All P1 tests pass (two consecutive green runs of `make test`; e2e suite about 13 s).

**Tests that must pass** (`tests/e2e/test_environment.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_nodes_ready` | 3 nodes, all `Ready=True` | ✅ |
| `test_environment_namespaces` | `dev`, `staging`, `prod` exist with `deploysim.io/simulator=true` | ✅ |
| `test_release_name_rule` | Server rejects a Release whose name ≠ `<app>-<version>` | ✅ |
| `test_release_spec_immutable` | Re-applying the identical Release succeeds; a different `image` is rejected with the CEL message | ✅ |
| `test_release_status_subresource` | Status patch persists `phase` and `attempts`; printer columns show them | ✅ |
| `test_stage_ready` | A plain 3-replica Deployment completes its rollout in under 10 s | ✅ |
| `test_stage_unhealthy` | `deploysim.io/fault: unhealthy` leads to `Progressing=False, ProgressDeadlineExceeded` within `progressDeadlineSeconds + 5 s`; pod `Running`, not Ready, `CrashLoopBackOff` | ✅ |
| `test_stage_slow_start` | `slow-start` pods are not Ready at 1 s, and are Ready after the delay read from `kwok/stages.yaml` | ✅ |
| `test_schemas_current` | Freshly exported schemas are byte-identical to `schemas/` | ✅ |

---

## P2: Audit log

**Status:** ✅ Done

**Goal:** a tamper-evident, append-only audit log that is safe under concurrent writers.

**Deliverables**
- `deploysim/audit.py`:
  - `AuditLog(path, clock)` with `append(Entry) -> AuditRecord`,
    `records(app=, environment=, deployment_id=)` and `verify() -> list[Problem]`;
  - `append` takes an exclusive `flock`, reads the last `seq` and `hash`, writes one JSON line, then
    flushes and fsyncs;
  - `append` refuses, with `AuditError`, to chain onto an incomplete last line;
  - `hash = sha256(prev_hash + canonical_json(record_without_hash))`, with a fixed genesis `prev_hash`;
  - `ts` is UTC ISO-8601 with milliseconds.

**Definition of done**
- [x] The record fields match the README audit example exactly, checked by
      `test_record_fields_match_readme`.
- [x] All P2 tests pass (15 tests, about 0.3 s).
- [x] Removing the `flock` makes `test_concurrent_appends` fail (3 of 3 runs), so the test guards the
      lock.

**Tests that must pass** (`tests/unit/test_audit.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_seq_gap_free` | 100 appends give `seq` 1..100 | ✅ |
| `test_first_record_starts_chain` | First record has `seq` 1 and the genesis `prev_hash` | ✅ |
| `test_chain_verifies` | `verify()` returns no problems on an untouched log | ✅ |
| `test_empty_log` | No file means no records and no problems | ✅ |
| `test_detects_edit` | Changing one field in one line is reported at exactly that line and `seq` | ✅ |
| `test_detects_delete` | Removing a middle line is reported as a `seq` gap and a broken `prev_hash` | ✅ |
| `test_detects_reorder` | Swapping two lines is reported | ✅ |
| `test_detects_truncation_mid_line` | A partial last line is reported, not silently skipped | ✅ |
| `test_append_refuses_after_incomplete_record` | `append` raises `AuditError` rather than chaining onto a partial line | ✅ |
| `test_concurrent_appends` | 8 processes, released together, × 50 appends give gap-free `seq`, a valid chain, and each writer's records in order | ✅ |
| `test_timestamp_format` | `ts` matches `YYYY-MM-DDTHH:MM:SS.mmmZ`; milliseconds are truncated | ✅ |
| `test_record_fields_match_readme` | Key order in memory and on disk equals the README audit example | ✅ |
| `test_append_fsyncs` | Every append calls `fsync` | ✅ |
| `test_records_filter` | Filtering by `app`, `environment` and `deployment_id` | ✅ |
| `test_unreadable_line_raises_on_records` | `records()` names the file and line of an unreadable record | ✅ |

---

## P3: Bundle loading and offline validation (pass 1)

**Status:** ✅ Done

**Goal:** `deploysim validate` rejects every invalid bundle listed in the README, with no cluster
access.

**Deliverables**
- `deploysim/paths.py`: project root (`$DEPLOYSIM_ROOT` or the current directory), and the paths to
  `bin/`, `schemas/`, `crds/` and `.deploysim/` under it.
- `deploysim/bundle.py`:
  - parse multi-document YAML, keeping empty documents so positions match kubectl-validate's output;
  - structure checks (before the schema): `EMPTY`, `NOT_AN_OBJECT`, `ONE_RELEASE`,
    `HAS_DEPLOYMENT`, `KIND_ALLOWED`;
  - `Bundle(release, objects)`, whose `digest` ignores document order, key order and YAML style;
  - policy rules (after the schema): `NO_NAMESPACE`, `NAME_LABEL`, `VERSION_LABEL`, `IMAGE_DIGEST`,
    `IMAGE_MATCHES_RELEASE` (container named `<app>`), `READINESS_PROBE`, `PROGRESS_DEADLINE`
    (unset counts as 600).
- `deploysim/validate.py`:
  - layers file, YAML, structure, schema, policy; the first failing layer ends validation;
  - runs `kubectl-validate --output json --local-schemas schemas/ --local-crds crds/` with
    `KUBECONFIG=/dev/null`;
  - maps each failure to `Violation(source, code, document, message)`. Unnamed documents are
    identified by position, and a CEL rule on the whole object is reported as `(root)`;
  - a missing binary, `schemas/` or `crds/` raises `ToolError`, which becomes exit code 5.
- `deploysim validate FILE`: JSON on stdout, `REJECTED` lines on stderr, exit 0, 1 or 5, and one
  audit record. `ExitCode` holds every exit code from the README.
- `examples/orders-api-1.0.0.yaml`, `examples/orders-api-1.1.0.yaml`, and
  `examples/orders-api-1.2.0.yaml` (with the `unhealthy` fault annotation). Each has a ConfigMap,
  Deployment and Service.
- `tests/fixtures/invalid/*.yaml`: 23 files, each one mutation of a valid bundle. Each starts with
  a description line and `# expect: <source>:<code>`.
- CI `unit` job: runs `make tools`, with `bin/` cached.

**Definition of done**
- [x] Every file in `examples/` passes, and so does the README's example bundle.
- [x] Every fixture fails with exactly the violations its header names, no more and no fewer.
- [x] Validation passes with no cluster running, and with both `KUBECONFIG` and `~/.kube/config`
      pointing at a hostile kubeconfig. A control in the same test shows that kubectl-validate,
      unisolated, does run that kubeconfig's credential plugin.
- [x] Validating one bundle takes under 2 s (about 0.1 s measured).
- [x] All P3 tests pass (33 in `test_validate.py`; 55 unit tests in about 5 s).

**Tests that must pass** (`tests/unit/test_validate.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_examples_valid[*]` | Each of the 3 examples has no violations and yields a `Bundle` | ✅ |
| `test_invalid_fixture[*]` | 23 fixtures; the set of violation keys equals the header's `# expect:` set. Covered: unknown field, wrong type, missing required field, malformed YAML, empty file, bad semver, tag instead of digest, name ≠ `<app>-<version>` (CEL), timeout out of range, zero or two Releases, no Deployment, a `Secret`, a non-map document, `metadata.namespace` set, missing name or version label, sidecar not pinned, app image ≠ Release image, no app container, missing `readinessProbe`, `progressDeadlineSeconds` too long or unset | ✅ |
| `test_isolated_from_kubeconfig` | With `KUBECONFIG` and `HOME` pointing at a kubeconfig whose exec plugin writes a marker file: the control run creates it, and deploysim's validation never does | ✅ |
| `test_bundle_digest_stable` | Document order, key order and YAML style give the same digest; changing `replicas` does not | ✅ |
| `test_validation_time` | One bundle validates in under 2 s | ✅ |
| `test_missing_file` | A missing file gives `file:READ` | ✅ |
| `test_cli_valid` | Exit 0, `VALID` JSON with release and digest, one `VALIDATE`/`OK` audit record carrying the kubectl-validate `cmd` | ✅ |
| `test_cli_rejected` | Exit 1, `REJECTED` JSON, stderr line, audit record listing the violation | ✅ |
| `test_tool_missing` | With an empty project root: exit 5, `ENVIRONMENT_ERROR`, "run make tools", audit record `ERROR` | ✅ |

---

## P4: Kubernetes access layer

**Status:** ✅ Done

**Goal:** typed, audited access to the cluster, with the safety guard, Lease locking and
compare-and-swap state.

**Deliverables**
- `deploysim/errors.py`: `EnvironmentProblem` (exit 5) with `ToolError` and `UnsafeTarget`.
- `deploysim/clock.py`: UTC clock; millisecond timestamps for audit and state; MicroTime for Leases.
- `deploysim/kube.py`, the only module that runs kubectl:
  - `Kubectl(binary, kubeconfig)` wraps `bin/kubectl`. Every call passes `--kubeconfig` and sets
    `$KUBECONFIG` to the same file;
  - `Kubectl.environment(name)` returns a namespace-bound `Environment` with `get`, `get_labelled`,
    `create`, `replace` (compare-and-swap via `resourceVersion`), `apply` (server-side,
    `--field-manager=deploysim`, `--force-conflicts`, optional `--dry-run=server`), `drift`,
    `delete` and `patch_status`;
  - every call returns its exact argv (`Result.cmd`) for the audit log;
  - stderr is classified into `NotFound`, `AlreadyExists`, `Conflict`, `Invalid`, `Forbidden` and
    `Unreachable` (exit 5). `Invalid` is checked before `Forbidden`, because immutable-field errors
    contain both words;
  - `drift(manifests)` compares each live object with a server-side dry-run apply made from the
    same `resourceVersion`, ignoring `.status` and `managedFields`. It retries objects a controller
    updated in between (up to 5 times). This replaced `kubectl diff`, which reported false changes
    in 2 of 15 runs.
- The safety guard, applied before any call:
  - the current-context server host must be `127.0.0.1`, `::1` or `localhost`, checked when
    `Kubectl` is constructed, before any process runs;
  - the target namespace must exist and be labelled `deploysim.io/simulator=true`, checked by
    `Kubectl.environment`;
  - a failure raises `UnsafeTarget`, which becomes exit code 5.
- `deploysim/lease.py` (all writes are compare-and-swap):
  - `acquire(env, app, holder, seconds, now)` creates `Lease deploysim-<app>`, takes over a
    released or expired one (`took_over_from`, `leaseTransitions` + 1), or raises `Locked`;
  - `renew(lease, now)`, which raises `Conflict` once another run has taken over (fencing);
  - `release(lease, now)` clears the holder, so the next run acquires immediately.
- `deploysim/state.py`: `State`, with fields in README order, plus `ReleaseRef`, `InFlight`,
  `Outcome` and `Status`. `StateStore.load()` returns an empty state when ConfigMap
  `deploysim-<app>` is missing. `StateStore.save()` creates it, or replaces it with the
  `resourceVersion` it read, and raises `Conflict` when stale, including a lost create race.

**Definition of done**
- [x] No module other than `kube.py` invokes `kubectl`. Only `kube.py` and `validate.py`
      (kubectl-validate) import `subprocess`, and a unit test enforces this.
- [x] All P4 tests pass (72 unit, 23 e2e; three consecutive green `make test` runs).
- [x] Drift checking never reports a change straight after an apply: 0 of 50 in a stress run
      (`kubectl diff`: 2 of 15), and 0 of 50 real changes missed.

**Tests that must pass**

| Test | Asserts | Status |
|---|---|---|
| `tests/unit/test_kube.py::test_guard_refuses_remote` | A kubeconfig pointing at `https://example.com:6443` raises `UnsafeTarget`; `subprocess.run` is never called | ✅ |
| `tests/unit/test_kube.py::test_guard_accepts_loopback[*]` | `127.0.0.1`, `localhost` and `[::1]` are accepted | ✅ |
| `tests/unit/test_kube.py::test_guard_missing_kubeconfig` | "run make cluster-up" | ✅ |
| `tests/unit/test_kube.py::test_guard_unknown_current_context` | A current context with no matching entry is refused | ✅ |
| `tests/unit/test_kube.py::test_missing_kubectl` | `ToolError` "run make tools" | ✅ |
| `tests/unit/test_kube.py::test_run_pins_kubeconfig` | argv has `--kubeconfig <file>`; `$KUBECONFIG` is overridden to the same file | ✅ |
| `tests/unit/test_kube.py::test_error_classification[*]` | 8 recorded kubectl stderr samples map to the right error type and message | ✅ |
| `tests/unit/test_kube.py::test_only_kube_and_validate_spawn_processes` | Only `kube.py` and `validate.py` import `subprocess` | ✅ |
| `tests/e2e/test_kube.py::test_guard_refuses_unlabelled_namespace` | A namespace without the label leads to `UnsafeTarget` | ✅ |
| `tests/e2e/test_kube.py::test_guard_refuses_missing_namespace` | A missing namespace leads to `UnsafeTarget` | ✅ |
| `tests/e2e/test_kube.py::test_lease_exclusive` | A second `acquire` raises `Locked` naming the holder | ✅ |
| `tests/e2e/test_kube.py::test_lease_takeover_after_expiry` | After `seconds`, a new holder acquires; `took_over_from` and `leaseTransitions` are set | ✅ |
| `tests/e2e/test_kube.py::test_lease_renew_extends` | A renewed Lease is still held past its original expiry | ✅ |
| `tests/e2e/test_kube.py::test_lease_fencing` | The old holder's `renew()` and `release()` after a takeover raise `Conflict`; the new holder keeps it | ✅ |
| `tests/e2e/test_kube.py::test_lease_release` | After `release()`, another holder acquires immediately | ✅ |
| `tests/e2e/test_kube.py::test_state_roundtrip` | A full state, including `in_flight`, survives save and load | ✅ |
| `tests/e2e/test_kube.py::test_state_cas` | Saving with a stale `resourceVersion` raises `Conflict`; the stored state is unchanged | ✅ |
| `tests/e2e/test_kube.py::test_state_create_race` | Two first saves: the second raises `Conflict` | ✅ |
| `tests/e2e/test_kube.py::test_apply_and_drift` | Applied unchanged: no drift; replicas changed: the Deployment drifts; new Service: listed | ✅ |
| `tests/e2e/test_kube.py::test_drift_ignores_controller_updates` | 20 fresh Deployments: never drift straight after apply | ✅ |
| `tests/e2e/test_kube.py::test_dry_run_rejects_invalid` | Server dry run raises `Invalid` with the CEL message | ✅ |
| `tests/e2e/test_kube.py::test_patch_status` | Release status is written through the status subresource | ✅ |

---

## P5: Deploy, happy path

**Status:** ✅ Done

**Goal:** `deploysim deploy` takes a valid release to `SUCCEEDED` and rejects everything pass 2
should reject.

**Deliverables**
- `deploysim/controller.py`:
  - `Controller.deploy(DeployRequest)` returns a `DeployReport` (outcome and summary). `Deploy`
    runs the stages up to holding the Lease. `Session` runs the stages that need the Lease, with
    the environment, bundle, state and `LeaseKeeper` always present;
  - pass-1 validation, then `LOCK`;
  - pass 2: degraded gate (`environment:DEGRADED`), promotion gate (`promotion:GATE`: the same
    Release spec and bundle digest `Succeeded` one environment earlier), immutability
    (`immutability:RELEASE_SPEC`, `immutability:BUNDLE`), then a server dry run
    (`server:DRY_RUN`) of the Release, the bundle ConfigMap and the workload objects;
  - `PREPARE`: write `in_flight`, create the immutable bundle ConfigMap
    `deploysim-<app>-<version>` (`objects.json`, digest annotation), apply the Release (digest
    annotation), set phase `Deploying`;
  - `DEPLOY`: apply the workload objects, then record `in_flight.stage = DEPLOY`;
  - `VERIFY`: phase `Verifying`, then `health.verify`, renewing the Lease on every poll
    (`LeaseLost` on `Conflict`);
  - `COMPLETE`: `current` and `last_known_good` set to the target, `in_flight` cleared, status
    `HEALTHY`, phase `Succeeded`;
  - the Lease is released at the end of every run, whatever the outcome.
- `deploysim/health.py`: `rollout_status` uses `kubectl rollout status`'s decision, and also
  fails on `ProgressDeadlineExceeded`. `verify` polls every Deployment, calls `on_poll` after each
  poll, and returns `Complete`, `ProgressDeadlineExceeded` or `Timeout`.
- `deploysim/outcomes.py`: `ExitCode`, moved from `cli.py`, and `Outcome` with its exit code.
  `VERIFY_FAILED` maps to 2 until P6 replaces it with `ROLLED_BACK`.
- `deployment_id` = `dep-` plus 12 hex of sha256(app | environment | defaulted Release spec |
  bundle digest). The attempt is the next number for that ID in `state.attempts`. The summary JSON
  goes to stdout, with one progress line per audit record on stderr. Exit codes 0, 1, 2
  (`VERIFY_FAILED`), 4 and 5.
- `deploysim status --app --env`: state, Releases with their phase, and the live rollout as JSON.
- Every object deploysim creates carries `app.kubernetes.io/name` and
  `app.kubernetes.io/managed-by: deploysim`.
- `Bundle.spec`: the Release spec with CRD defaults, so a Release without `healthCheck` compares
  equal to the server's copy.
- Test support: `write_bundle`, a `deploysim` CLI runner, and the `deploy` and `audit_log`
  fixtures.

**Out of scope:** a failing `VERIFY` exits 2 (`VERIFY_FAILED`), leaves `in_flight` in place and
the Release in `Verifying`. Rollback is P6. `test_verify_failure_stops` asserts only what stays
true after P6: exit 2, a `FAIL` VERIFY record, and `current` unchanged.

**Definition of done**
- [x] The README walkthrough's first four commands run as written (validate 1.0.0; deploy 1.0.0 to
      dev and staging; deploy 1.1.0 to dev; all exit 0).
- [x] Every stage writes at least one audit record with `cmd`, and the audit chain verifies.
      `history --verify` is P9; `test_audit_trail` uses `AuditLog.verify()`.
- [x] A `REJECTED` run creates no workload objects, Releases or ConfigMaps. Only the Lease is
      touched.
- [x] All P5 tests pass (84 unit, 36 e2e; three consecutive green `make test` runs; e2e about 60 s).

**Tests that must pass**

| Test | Asserts | Status |
|---|---|---|
| `tests/e2e/test_deploy.py::test_first_deploy` | Exit 0; `SUCCEEDED`; Release `Succeeded` with attempts 1; state `HEALTHY`, `current == last_known_good`, no `in_flight`; bundle ConfigMap immutable with digest; Lease released; managed-by label | ✅ |
| `tests/e2e/test_deploy.py::test_upgrade` | 1.0.0 then 1.1.0: `from` and `to` correct; every live pod runs the 1.1.0 image | ✅ |
| `tests/e2e/test_deploy.py::test_promotion_chain` | dev → staging → prod all exit 0; both gates `OK`; Release `Succeeded` in all three | ✅ |
| `tests/e2e/test_deploy.py::test_slow_start` | Exit 0; VERIFY waited at least 4 s, over several polls | ✅ |
| `tests/e2e/test_deploy.py::test_audit_trail` | Stages first appear in order VALIDATE, LOCK, PREPARE, DEPLOY, VERIFY, COMPLETE, and end with LOCK; `seq` is gap-free and equals the summary steps; every stage has a record with `cmd`; chain verifies | ✅ |
| `tests/e2e/test_deploy.py::test_verify_failure_stops` | An `unhealthy` upgrade: exit 2, VERIFY `FAIL` with `ProgressDeadlineExceeded`, `current` still the previous release | ✅ |
| `tests/e2e/test_rejections.py::test_invalid_bundle_creates_nothing` | Exit 1, `bundle:READINESS_PROBE`; no objects with the app label, not even a Lease | ✅ |
| `tests/e2e/test_rejections.py::test_promotion_gate` | Staging before dev: exit 1, `promotion:GATE`, nothing created in staging | ✅ |
| `tests/e2e/test_rejections.py::test_promotion_gate_needs_same_bundle` | Same Release, different replicas than dev: `promotion:GATE` | ✅ |
| `tests/e2e/test_rejections.py::test_version_reused_different_image` | Exit 1, `immutability:RELEASE_SPEC`; the live Release keeps the original image | ✅ |
| `tests/e2e/test_rejections.py::test_version_reused_different_manifests` | Same Release, changed Deployment: `immutability:BUNDLE`; live replicas unchanged | ✅ |
| `tests/e2e/test_rejections.py::test_locked` | With the Lease held by another holder: exit 4, `LOCKED` naming the holder, nothing created, holder unchanged | ✅ |
| `tests/e2e/test_rejections.py::test_degraded_gate` | `DEGRADED` state: exit 1 `environment:DEGRADED`; with `--allow-degraded`: exit 0 and `HEALTHY` | ✅ |
| `tests/unit/test_controller.py::test_deployment_id_stable` | Stable; ignores defaulted `healthCheck`; changes with environment or bundle | ✅ |
| `tests/unit/test_controller.py::test_in_cluster_manifests_carry_labels_and_digest` | Release, bundle ConfigMap and workloads carry the labels; digest annotations; the bundle itself is not mutated | ✅ |
| `tests/unit/test_controller.py::test_outcome_exit_codes` | Every `Outcome` maps to the README exit code | ✅ |
| `tests/unit/test_health.py::test_rollout_status[*]` | Six rollout states decided like `kubectl rollout status` | ✅ |
| `tests/unit/test_health.py::test_verify_*` | VERIFY waits until complete (calling `on_poll` every poll), fails fast on the deadline, times out | ✅ |

---

## P6: Rollback and degraded environments

**Status:** ✅ Done

**Goal:** a failed `VERIFY` always ends in `ROLLED_BACK` or `ROLLBACK_FAILED`, never in between.

**Deliverables**
- `Session.rollback()`, which runs when `VERIFY` fails:
  - records `in_flight.stage = ROLLBACK`;
  - with no `last_known_good` (a first deploy), deletes the bundle's objects (`--wait=true`). The
    environment becomes `EMPTY`, the outcome `ROLLED_BACK` (exit 2), the phase `RolledBack`;
  - otherwise loads `last_known_good`'s stored objects and its timeout (`load_bundle`),
    re-applies them, then verifies with that timeout:
    - check passes: `HEALTHY`, `ROLLED_BACK` (exit 2), phase `RolledBack`;
    - check fails: `DEGRADED`, `ROLLBACK_FAILED` (exit 3), phase `RollbackFailed`;
  - always clears `in_flight` and records `last_outcome`. The summary's `rollback` reports
    `performed`, `to` and `result`.
- The intent record's `previous` is now `last_known_good`, not `current`, so a rollback always
  returns to a known-good release, even after a forced deploy into a `DEGRADED` environment.
- `deploy --inject-fault unhealthy|unhealthy-rollback`. `workload_manifests(objects, app, fault)`
  annotates Deployment pod templates at apply time only; the stored bundle stays unannotated. The
  `DEPLOY` and `ROLLBACK` apply records name the fault injected.
- `VERIFY_FAILED` is removed from `Outcome`. The degraded gate and `--allow-degraded` were built in
  P5.

**Definition of done**
- [x] No `xfail` markers were needed. P5's `test_verify_failure_stops` now selects VERIFY's
      `rollout_status` step, since ROLLBACK adds its own.
- [x] After every rollback test, `state.in_flight is None` and `status` is `HEALTHY`, `EMPTY` or
      `DEGRADED` (`assert_settled`).
- [x] All P6 tests pass (86 unit, 43 e2e; three consecutive green `make test` runs; e2e about
      105 s).

**Tests that must pass** (`tests/e2e/test_rollback.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_unhealthy_upgrade_rolls_back` | `--inject-fault unhealthy`: exit 2, `ROLLED_BACK`, `final == from`, rollback `SUCCEEDED` to 1.0.0, `HEALTHY`, phase `RolledBack`; every pod back on the 1.0.0 image | ✅ |
| `test_old_pods_keep_serving` | Pods sampled every 0.2 s during the failed rollout: faulty pods appeared, and ready pods of the original ReplicaSet never dropped below `replicas - maxUnavailable` (4 - 1) | ✅ |
| `test_broken_build` | A fault in the bundle itself rolls back to 1.0.0 (exit 2, `HEALTHY`); that stored bundle does carry the annotation | ✅ |
| `test_first_deploy_unhealthy` | Exit 2; rollback to `None`; `EMPTY`; no Deployment or Service with the app label remains; phase `RolledBack` | ✅ |
| `test_rollback_fails` | `unhealthy-rollback`: exit 3, `ROLLBACK_FAILED`, rollback `FAILED` to 1.0.0, `DEGRADED`, phase `RollbackFailed`; one ROLLBACK `rollout_status` `FAIL` | ✅ |
| `test_degraded_gate` | After a real failed rollback: the next deploy gives exit 1 `environment:DEGRADED`; with `--allow-degraded`, exit 0 and `HEALTHY` on 1.1.0 | ✅ |
| `test_stored_bundle_unannotated` | The bundle ConfigMap of an injected-fault release has no `deploysim.io/fault`; the DEPLOY record shows `injected_fault: unhealthy` | ✅ |
| `tests/unit/test_controller.py::test_fault_injected_only_into_pod_templates` | Only Deployment pod templates gain the annotation; the bundle and its ConfigMap stay clean | ✅ |
| `tests/unit/test_controller.py::test_fault_targets` | `unhealthy` faults the target only; `unhealthy-rollback` faults both | ✅ |

---

## P7: Idempotency, drift and retries

**Status:** ✅ Done

**Goal:** re-running any deploy is safe, and converges on the desired state.

**Deliverables**
- `Session.desired_state()`, run after pass 2 and before `PREPARE`, records one `desired_state`
  step:
  - `DEPLOY` when the target is not current, an intent record exists, or the status is not
    `HEALTHY`;
  - `NO_OP` (exit 0) when the target is current, `Environment.drift()` reports nothing, and
    every Deployment's rollout is complete;
  - `DRIFT` when the target is current but drifted or is unhealthy. It then redeploys as the next
    attempt, with `drift_corrected: true` in the summary.
- `attempt` is assigned at `PREPARE` (`attempts[deployment_id] + 1`), so `REJECTED`, `LOCKED` and
  `NO_OP` report `attempt: null`.
- `record_intent` carries `downgrade`, comparing versions by semantic-version precedence
  (`bundle.version_key`).

**Definition of done**
- [x] A `NO_OP` run performs no write. Its steps contain none of the write actions, and the state
      ConfigMap, Release, Deployment generation and ReplicaSets are unchanged. Only the Lease's
      holder and renew time change, because the check runs under the lock.
- [x] All P7 tests pass (88 unit, 48 e2e; three consecutive green `make test` runs; e2e about
      2 min). `test_concurrent_deploys` also passed 8 of 8 standalone runs.

**Tests that must pass** (`tests/e2e/test_idempotency.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_repeat_is_noop` | Second identical deploy: exit 0, `NO_OP`, `attempt: null`, no write step; the state and Release resourceVersions, Deployment generation and ReplicaSet set are unchanged; attempts still 1 | ✅ |
| `test_drift_corrected` | After `kubectl scale --replicas=1`: exit 0, `SUCCEEDED`, `drift_corrected: true`, attempt 2, `desired_state` lists `Deployment/<app>`; replicas restored to 2 | ✅ |
| `test_retry_after_rollback` | `unhealthy`, then the same release without the fault: same deployment ID, attempts 1 then 2; Release `Succeeded` with attempts 2 | ✅ |
| `test_concurrent_deploys` | Two deploys started together: exit codes `[0, 4]`; the loser is `LOCKED`; state `HEALTHY` on the winner's release, no `in_flight`, Lease released, audit chain intact | ✅ |
| `test_downgrade_flagged` | 1.0.0, then 1.1.0, then 1.0.0: `downgrade` is `[False, False, True]` | ✅ |
| `tests/unit/test_bundle.py::test_version_ordering` | The Semantic Versioning specification's precedence example, then later releases, sort correctly | ✅ |

---

## P8: Crash recovery

**Status:** ✅ Done

**Goal:** a run that dies at any stage boundary leaves a state the next run resolves.

**Deliverables**
- The controller is restructured:
  - `Invocation` is the base for one CLI command: it maps environment problems to exit 5,
    takes the Lease, and always releases it;
  - `Deploy` and `Recover` extend it;
  - `Operator` holds the environment, app, state and Lease, plus the helpers deploy and recover
    share (`_reapply`, `_remove`, `_await_rollout`, `_mark_release`, `_finish`, `recover`);
  - `Session` extends `Operator` with the deploy stages.

  The P5–P7 e2e suite passed unchanged on the refactor.
- `Operator.recover()` (`RECOVER` stage) runs right after `load_state` when an intent record
  exists (the Lease was acquired, so its run is dead):
  - records `find_intent`;
  - re-applies `in_flight.previous` and verifies it, or, with no previous, removes the target's
    stored objects (`EMPTY`);
  - clears `in_flight`, records `last_outcome` `INTERRUPTED`, and sets the interrupted Release's
    phase to `Interrupted`. If the Release was never created, that is recorded as `MISSING`;
  - restore check passes: the requested deploy continues as the next attempt. It fails:
    `DEGRADED`, `ROLLBACK_FAILED` (exit 3), and the deploy stops.
- `deploysim recover --app --env [--lease-seconds]`:
  - `RECOVERED` (exit 0, new `Outcome`);
  - `NO_OP` with nothing to recover;
  - `LOCKED` while the Lease is live;
  - `ROLLBACK_FAILED` if the restore fails.
- `--inject-fault crash-after-prepare | crash-after-deploy | crash-during-rollback`.
  `Session.crash_point()` records `inject_crash`, then calls `os._exit(70)`
  (`ExitCode.SIMULATED_CRASH`) without releasing the Lease or printing a summary.
  `crash-during-rollback` also makes the target unhealthy, so the rollback happens.
- The summary gains a `recovery` block (`performed`, `interrupted`, `to`, `result`).
- `--lease-seconds` was already available on `deploy`, and is now on `recover` too.

**Definition of done**
- [x] For every crash point, a run after Lease expiry ends with `in_flight is None` and status
      `HEALTHY` (crash points × `test_deploy_recovers`).
- [x] All P8 tests pass (89 unit, 60 e2e; three consecutive green `make test` runs; e2e about
      2 min 50 s).

**Tests that must pass** (`tests/e2e/test_recovery.py`)

| Test | Asserts | Status |
|---|---|---|
| `test_crash_leaves_intent[after-prepare, after-deploy, during-rollback]` | Exit 70, no summary, `inject_crash` on stderr; `DEPLOYING`; `in_flight` names target 1.1.0, previous 1.0.0, and the stage (`PREPARE`, `DEPLOY`, `ROLLBACK`) | ✅ |
| `test_locked_while_lease_live[*]` | A deploy straight after the crash gives exit 4, `LOCKED` | ✅ |
| `test_deploy_recovers[*]` | After `--lease-seconds 3` expires: exit 0, `SUCCEEDED`, attempt 2; `recovery` names the interrupted ID, `to` 1.0.0, `SUCCEEDED`; `HEALTHY` on 1.1.0, no `in_flight`; RECOVER records start with `find_intent` and mark `Interrupted`; audit chain intact | ✅ |
| `test_recover_command` | `recover` while the Lease is live gives exit 4; after expiry, `RECOVERED` to 1.0.0, `HEALTHY`, `last_outcome` `INTERRUPTED`, Release 1.1.0 `Interrupted`, pods back on 1.0.0 | ✅ |
| `test_recover_first_deploy` | A crash after the first DEPLOY, then `recover`: `EMPTY`, no Deployment or Service, Release `Interrupted` | ✅ |
| `test_recover_noop` | `recover` with no `in_flight`: exit 0, `NO_OP`, `recovery.performed` false; state ConfigMap resourceVersion unchanged | ✅ |
| `tests/unit/test_controller.py::test_crash_faults` | `crash-during-rollback` makes the target unhealthy; other crash points do not; `SIMULATED_CRASH` is 70 | ✅ |

---

## P9: CLI completeness and demo

**Status:** ✅ Done

**Goal:** every command and exit code in the README behaves as documented, and the demo exercises
them all.

**Deliverables**
- `deploysim history`: JSON lines filtered by `--app`, `--env` and `--deployment-id`. `--verify`
  checks the whole log and prints `{audit_log, records, verified, problems}`; it exits 1 if the
  log was altered, with `ALTERED line N` on stderr.
- `cli.Parser` overrides `ArgumentParser.error` so usage errors exit 64, including no command at
  all.
- `scripts/demo.sh`, run by `make demo`: 23 scenarios on copies of the examples under
  `demo-<timestamp>`, with deadlines cut to 10 s and 20 s. Each asserts its exit code, covering
  0, 1, 2, 3, 4, 5, 64 and 70 and every outcome. It deletes its app afterwards, unless `KEEP=1`,
  and runs another CLI if `DEPLOYSIM` is set (for P10's wheel install). It needs `jq`.
- `make test`: one pytest run over `tests/unit` and `tests/e2e` with `--cov-fail-under=85`.
  Coverage measures the CLI subprocesses (`[tool.coverage.run] patch = ["subprocess"]`).
- `tests/e2e/test_deploy.py::test_status` covers `status`'s success path.

**Definition of done**
- [x] `make cluster-down cluster-up demo` passes from scratch: all 23 scenarios, in about 85 s.
      This ran on a separate throwaway cluster (`CLUSTER_NAME=deploysim-scratch`, in its own
      directory with its own `.deploysim/`), so the working cluster and its deployments were
      untouched. The throwaway cluster was deleted afterwards.
- [x] Every command in the README walkthrough runs verbatim on that fresh cluster, with the
      documented exit codes (0, 0, 0, 0, 2, 2, 0, 0, 0) and end states (staging `HEALTHY` on
      1.0.0, dev `HEALTHY` on 1.1.0).
- [x] All P9 tests pass: 165 tests; coverage 96.1% against the 85% gate.

**Tests that must pass**

| Test | Asserts | Status |
|---|---|---|
| `tests/unit/test_cli.py::test_usage_error_64[*]` | No command, missing file, missing `--env`, invalid `--env`, unknown flag, invalid `history --env`, `recover` without `--env`: exit 64, `usage:` on stderr | ✅ |
| `tests/unit/test_cli.py::test_summary_schema` | The deploy summary's keys equal the README example's keys, in order; values match the documented types; `rollback`, `recovery` and step shapes too | ✅ |
| `tests/unit/test_cli.py::test_history_verify_exit` | Intact log: exit 0, `verified: true`, 3 records. Tampered line 2: exit 1, `ALTERED line 2` | ✅ |
| `tests/unit/test_cli.py::test_history_filters` | `--app`, and `--app` with `--env`, select the right `seq`s | ✅ |
| `tests/unit/test_cli.py::test_unsafe_target_exit_5` | `deploy` and `status` with a `prod.example.com` kubeconfig: exit 5, "refusing" | ✅ |
| `tests/unit/test_readme.py::test_exit_codes_match` | The README exit-code table equals `ExitCode`; deleting the 70 row makes it fail | ✅ |
| `tests/unit/test_readme.py::test_outcomes_documented` / `test_commands_documented` / `test_faults_documented` | Every `Outcome`, subcommand and `--inject-fault` value appears in the README | ✅ |
| `tests/e2e/test_deploy.py::test_status` | `status` JSON: state `HEALTHY`, Release `Succeeded` with attempts 1, rollout `2/2 available` | ✅ |
| `make demo` | Exit 0; all 23 scenarios as expected | ✅ |
| `make test` | 165 passed; coverage ≥ 85% (96.1%) | ✅ |

---

## P10: CI complete

**Status:** ✅ Done locally; the green run on GitHub and the wall time are confirmed on this phase's
PR

**Goal:** the README's CI workflow, running green on GitHub.

**Deliverables**
- `.github/workflows/ci.yml`:
  - jobs `lint`, `unit` (Python 3.11 and 3.14), `manifests` and `package` start in parallel; `e2e`
    needs only `package`;
  - every action is pinned to the commit SHA of a release at least two weeks old;
  - `bin/` and `~/.kwok/cache` are cached, keyed on the pinned versions.
- `manifests`: `kubectl-validate crds/` (offline) plus `deploysim validate` on every example.
- `package` runs `make package`:
  - wheel and sdist;
  - a CycloneDX SBOM generated from a venv with the wheel installed;
  - `SHA256SUMS`;
  - on pushes to `main`, `actions/attest-build-provenance` for the wheel and sdist (the job alone
    gets `id-token: write` and `attestations: write`);
  - uploaded as the `dist` artifact.
- `e2e`:
  - installs the `dist` wheel and pytest into a clean venv;
  - moves the checkout's `deploysim/` aside and asserts that `import deploysim` resolves to
    site-packages, so the tests and demo run the wheel;
  - `make cluster-up` (its CRD apply is the server-side check);
  - `make schemas` and `git diff --exit-code schemas/`;
  - `pytest -m e2e tests/e2e`, then `scripts/demo.sh` with `DEPLOYSIM` and `AUDIT_LOG`;
  - on failure, uploads `e2e-diagnostics` (component logs, events, objects, every audit log);
  - always runs `make cluster-down`.
- `make lint` adds `actionlint` (`actionlint-py`, pinned).
- `scripts/demo.sh` honours `AUDIT_LOG`.

**Changes from the original plan**
- The separate CRD server dry-run step was dropped. `make cluster-up` already applies
  `crds/release.yaml` to the API server, so an invalid CRD fails `e2e` there. `manifests` also
  catches it offline.
- Correction to P1's finding: kubectl-validate does reject the invalid CRD when validating the
  CRD document. It only accepts it when loading it through `--local-crds`.
- By your choice, the negative checks were proven locally rather than through throwaway PRs.

**Definition of done**
- [ ] The workflow is green on a pull request and on `main` (this phase's PR).
- [x] `actionlint` is clean, and runs in `make lint`.
- [ ] Full workflow wall time is under 10 minutes (measured on this phase's PR).
- [x] Each negative check fails the step it names, verified locally:
      - broken example: the `manifests` loop exits 1;
      - stale schemas: `git diff --exit-code` exits 1;
      - `additionalProperties` beside `properties` in the CRD: `kubectl-validate crds/` exits 1,
        and the server apply exits 1.
      Diagnostics upload is only exercised by a real failing CI run.
- [x] The e2e job's steps, replayed locally in a clean copy on a throwaway cluster
      (`deploysim-ci`): the wheel resolves from site-packages, schemas match, 61 e2e tests pass,
      and the demo passes 23 of 23.

**Tests that must pass**

| Check | Expected | Status |
|---|---|---|
| Green run on a PR | All jobs pass | ⏳ this phase's PR |
| A PR breaking an example bundle | `manifests` fails | ✅ local: exit 1 |
| A PR with stale `schemas/` | `e2e` fails at `git diff --exit-code` | ✅ local: exit 1 |
| A PR adding `additionalProperties: false` beside `properties` in the CRD | `manifests` fails (`kubectl-validate crds/`); `e2e` fails at `make cluster-up` | ✅ local: both exit 1 |
| A PR failing an e2e test | Diagnostics artifacts are uploaded; the cluster is deleted | ⏳ only on a real failing run |
| Replay of the `e2e` job against the wheel | 61 e2e passed; demo 23 of 23 | ✅ |

---

## P11: Documentation pass

**Goal:** the README describes the system as built.

**Deliverables**
- The README tests table includes `test_kube.py`, `test_controller.py` and
  `test_readme.py`.
- `--inject-fault` lists the crash points added in P8.
- Example outputs (summary, audit line, state, `kubectl get releases`) are replaced with real output
  from `make demo`.

**Definition of done**
- [ ] A reader following only the README, on a clean macOS or Linux machine, reaches a green `make demo`.
- [ ] `tests/unit/test_readme.py` passes.
- [ ] No README claim lacks a test, except the production controls section, which is design only.
