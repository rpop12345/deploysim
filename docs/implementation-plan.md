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

These are checked again at the end of each phase. They have been met for P0 to P3.

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
| P3 Bundle loading and offline validation | ✅ Done | not yet committed |
| P4 Kubernetes access layer | ⬜ Not started | |
| P5 Deploy, happy path | ⬜ Not started | |
| P6 Rollback and degraded environments | ⬜ Not started | |
| P7 Idempotency, drift and retries | ⬜ Not started | |
| P8 Crash recovery | ⬜ Not started | |
| P9 CLI completeness and demo | ⬜ Not started | |
| P10 CI complete | ⬜ Not started | |
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

**Goal:** typed, audited access to the cluster, with the safety guard, Lease locking and
compare-and-swap state.

**Deliverables**
- `deploysim/kube.py`:
  - `Kubectl(kubeconfig)` wrapping `bin/kubectl`, with the methods `get_json`, `apply`
    (server-side, `--field-manager=deploysim`), `apply_dry_run`, `diff` (exit code to bool),
    `create`, `replace` (carries `resourceVersion`), `delete`, `patch_status`;
  - every call returns its exact argv for the audit log;
  - stderr is classified into `NotFound`, `AlreadyExists`, `Conflict`, `Invalid`, `Unreachable`
    and `Forbidden`.
- The safety guard, applied before any call:
  - the current-context server host must be `127.0.0.1`, `::1` or `localhost`;
  - the target namespace must be labelled `deploysim.io/simulator=true`;
  - a failure raises `UnsafeTarget`, which becomes exit code 5.
- `deploysim/lease.py` (all writes are compare-and-swap):
  - `acquire(holder, seconds)` creates the Lease, takes over an expired one, or raises `Locked`;
  - `renew()`;
  - `release()` clears the holder.
- `deploysim/state.py`: a `State` dataclass matching the README, with `load()` (empty state when the
  ConfigMap is missing) and `save()` carrying `resourceVersion`, which raises `Conflict` when stale.

**Definition of done**
- [ ] No module other than `kube.py` invokes `kubectl`.
- [ ] All P4 tests pass.

**Tests that must pass**

| Test | Asserts |
|---|---|
| `tests/unit/test_kube.py::test_guard_refuses_remote` | A kubeconfig pointing at `https://example.com` raises `UnsafeTarget` before any subprocess runs |
| `tests/unit/test_kube.py::test_error_classification[*]` | Recorded kubectl stderr samples map to the right error type |
| `tests/e2e/test_kube.py::test_guard_refuses_unlabelled_namespace` | Namespace without the label leads to `UnsafeTarget` |
| `tests/e2e/test_kube.py::test_lease_exclusive` | A second `acquire` raises `Locked` |
| `tests/e2e/test_kube.py::test_lease_takeover_after_expiry` | After `seconds` elapse, a new holder acquires the Lease |
| `tests/e2e/test_kube.py::test_lease_fencing` | The old holder's `renew()` after a takeover raises `Conflict` |
| `tests/e2e/test_kube.py::test_lease_release` | After `release()`, another holder acquires immediately |
| `tests/e2e/test_kube.py::test_state_roundtrip` | Save then load gives an identical `State` |
| `tests/e2e/test_kube.py::test_state_cas` | Saving with a stale `resourceVersion` raises `Conflict`; the stored state is unchanged |

---

## P5: Deploy, happy path

**Goal:** `deploysim deploy` takes a valid release to `SUCCEEDED` and rejects everything pass 2
should reject.

**Deliverables**
- `deploysim/controller.py`, implementing these stages:
  - pass-1 validation, then `LOCK`;
  - pass-2 validation: environment, promotion gate, degraded gate, immutability, server dry run;
  - `PREPARE`: write `in_flight`, create the immutable bundle ConfigMap, apply the Release with
    phase `Deploying`;
  - `DEPLOY`, `VERIFY`, `COMPLETE`, then release the Lease.
- `deploysim/health.py`:
  - polls each Deployment and renews the Lease on every poll;
  - passes when the rollout is complete;
  - fails on `ProgressDeadlineExceeded` or when the timeout elapses.
- `deployment_id` derivation, attempt counting, the summary JSON on stdout, progress lines on stderr,
  and exit codes 0, 1, 4 and 5.
- `deploysim status`.
- Every object deploysim creates carries the labels `app.kubernetes.io/name` and
  `app.kubernetes.io/managed-by: deploysim`.

**Out of scope:** a failing `VERIFY` exits 2 and leaves `in_flight` in place. Rollback is P6, and
P6's tests are marked `xfail` until then.

**Definition of done**
- [ ] The README walkthrough's first four commands run as written.
- [ ] Every stage writes at least one audit record with `cmd`, and `history --verify` passes.
- [ ] A `REJECTED` run creates no workload objects, Releases or ConfigMaps.
- [ ] All P5 tests pass.

**Tests that must pass**

| Test | Asserts |
|---|---|
| `tests/e2e/test_deploy.py::test_first_deploy` | Exit 0; outcome `SUCCEEDED`; Release `Succeeded`, attempts 1; `current == last_known_good`; bundle ConfigMap immutable; Lease released |
| `tests/e2e/test_deploy.py::test_upgrade` | 1.0.0 then 1.1.0; `from`/`to` correct; pods run the 1.1.0 image |
| `tests/e2e/test_deploy.py::test_promotion_chain` | dev → staging → prod all exit 0 |
| `tests/e2e/test_deploy.py::test_slow_start` | Exit 0; `VERIFY` waited at least the stage delay |
| `tests/e2e/test_deploy.py::test_audit_trail` | Stages appear in order with gap-free `seq`; `history --verify` exits 0 |
| `tests/e2e/test_rejections.py::test_invalid_bundle_creates_nothing` | Exit 1; no objects with the app label exist |
| `tests/e2e/test_rejections.py::test_promotion_gate` | Staging before dev gives exit 1 with a gate violation |
| `tests/e2e/test_rejections.py::test_version_reused_different_image` | Exit 1; the violation cites the CEL immutability message from the server dry run |
| `tests/e2e/test_rejections.py::test_version_reused_different_manifests` | Same Release, changed Deployment, gives exit 1 (immutable bundle ConfigMap) |
| `tests/e2e/test_rejections.py::test_locked` | With the Lease held by another holder: exit 4, no other change |
| `tests/unit/test_controller.py::test_deployment_id_stable` | Same inputs give the same ID; a different environment or digest gives a different ID |

---

## P6: Rollback and degraded environments

**Goal:** a failed `VERIFY` always ends in `ROLLED_BACK` or `ROLLBACK_FAILED`, never in between.

**Deliverables**
- `ROLLBACK`:
  - re-apply the last known-good bundle from its ConfigMap, then verify it;
  - with no previous release, delete the bundle's objects and set `EMPTY`;
  - when rollback verification fails, set `DEGRADED` and exit 3.
- `--inject-fault unhealthy` and `--inject-fault unhealthy-rollback` add the fault annotation at
  apply time only. The stored bundle stays unannotated.
- The degraded gate and `--allow-degraded`.
- Release phases `RolledBack` and `RollbackFailed`.

**Definition of done**
- [ ] The P5 `xfail` markers are removed.
- [ ] After every test, `state.in_flight is None` and `status` is one of `HEALTHY`, `EMPTY` or `DEGRADED`.
- [ ] All P6 tests pass.

**Tests that must pass** (`tests/e2e/test_rollback.py`)

| Test | Asserts |
|---|---|
| `test_unhealthy_upgrade_rolls_back` | Exit 2; `final == from`; Release `RolledBack`; `environment_status` `HEALTHY` |
| `test_old_pods_keep_serving` | During the failed rollout, the original ReplicaSet never drops below `replicas - maxUnavailable` ready pods |
| `test_broken_build` | `orders-api-1.2.0` gives exit 2 in dev |
| `test_first_deploy_unhealthy` | Exit 2; `status` `EMPTY`; no Deployment or Service with the app label remains |
| `test_rollback_fails` | `unhealthy-rollback` gives exit 3; `status` `DEGRADED`; Release `RollbackFailed` |
| `test_degraded_gate` | The next deploy gives exit 1; with `--allow-degraded` a healthy release gives exit 0 and `HEALTHY` |
| `test_stored_bundle_unannotated` | The bundle ConfigMap has no `deploysim.io/fault` after an injected-fault run |

---

## P7: Idempotency, drift and retries

**Goal:** re-running any deploy is safe, and converges on the desired state.

**Deliverables**
- The desired-state check: target is current, `kubectl diff` exits 0, and the rollout is healthy,
  giving `NO_OP`.
- Drift: when the target is current but differs or is unhealthy, re-apply and verify, and set
  `drift_corrected: true`.
- Attempt N+1 on retry after a rollback; the `downgrade` flag.

**Definition of done**
- [ ] A `NO_OP` run performs no write call. The audit records show only reads.
- [ ] All P7 tests pass.

**Tests that must pass** (`tests/e2e/test_idempotency.py`)

| Test | Asserts |
|---|---|
| `test_repeat_is_noop` | Second identical deploy gives exit 0 and `NO_OP`; Deployment `generation` and ReplicaSet set unchanged |
| `test_drift_corrected` | After `kubectl scale --replicas=1`, deploy gives exit 0 with `drift_corrected: true` and replicas restored |
| `test_retry_after_rollback` | `unhealthy` then the same release without the fault gives exit 0 and attempt 2, in both the summary and the Release status |
| `test_concurrent_deploys` | Two deploys started together: exactly one exit 0 and one exit 4; final state consistent |
| `test_downgrade_flagged` | 1.1.0 then 1.0.0 gives exit 0, and the audit records `downgrade: true` |

---

## P8: Crash recovery

**Goal:** a run that dies at any stage boundary leaves a state the next run resolves.

**Deliverables**
- `RECOVER`: runs when `in_flight` is present and the Lease was acquired. It rolls back to
  `in_flight.previous`, or to `EMPTY`, verifies, sets the Release phase `Interrupted`, then continues
  with the requested deploy.
- `deploysim recover --app --env`.
- `--inject-fault` gains `crash-after-prepare`, `crash-after-deploy` and `crash-during-rollback`.
  Each calls `os._exit(70)` without releasing the Lease. The README is updated in the same commit.
- `--lease-seconds`.

**Definition of done**
- [ ] For every crash point, a run after Lease expiry ends with `in_flight is None` and a named `status`.
- [ ] All P8 tests pass.

**Tests that must pass** (`tests/e2e/test_recovery.py`)

| Test | Asserts |
|---|---|
| `test_crash_leaves_intent[*]` | Parametrised over the 3 crash points: exit 70; `status` `DEPLOYING`; `in_flight` names the target and previous release |
| `test_locked_while_lease_live` | A deploy immediately after the crash gives exit 4 |
| `test_deploy_recovers[*]` | After `--lease-seconds 3` expires, deploy gives recovery records in the audit log, then exit 0 with attempt 2 |
| `test_recover_command` | `recover` restores last known-good; Release `Interrupted`; `status` `HEALTHY` |
| `test_recover_first_deploy` | A crash on the first deploy, then `recover`, gives `EMPTY` with no workload objects |
| `test_recover_noop` | `recover` with no `in_flight` gives exit 0 and changes nothing |

---

## P9: CLI completeness and demo

**Goal:** every command and exit code in the README behaves as documented, and the demo exercises
them all.

**Deliverables**
- `deploysim history`, with `--app`, `--env`, `--deployment-id` filters and `--verify`.
- Usage errors exit 64 (an `argparse` override).
- `scripts/demo.sh`: runs the README walkthrough plus each failure scenario and asserts every exit
  code. Exposed as `make demo`.
- A coverage gate of 85% in `make test`.

**Definition of done**
- [ ] `make cluster-down cluster-up demo` passes from scratch.
- [ ] Every command in the README runs verbatim.
- [ ] All P9 tests pass.

**Tests that must pass**

| Test | Asserts |
|---|---|
| `tests/unit/test_cli.py::test_usage_error_64` | An unknown flag or missing argument exits 64 |
| `tests/unit/test_cli.py::test_summary_schema` | The deploy summary has exactly the README's keys and types |
| `tests/unit/test_cli.py::test_history_verify_exit` | A tampered log makes `history --verify` exit non-zero |
| `tests/unit/test_cli.py::test_unsafe_target_exit_5` | A non-loopback kubeconfig exits 5 |
| `tests/unit/test_readme.py::test_exit_codes_match` | The README exit-code table equals the `ExitCode` enum |
| `make demo` | Exit 0 |
| `make test` | Coverage ≥ 85% |

---

## P10: CI complete

**Goal:** the README's CI workflow, running green on GitHub.

**Deliverables**
- `.github/workflows/ci.yml` jobs `lint`, `unit`, `manifests`, `package` and `e2e`, as described in
  the README CI section.
- Caches for `bin/` and `~/.kwok/cache`, keyed on the pinned versions.
- `e2e` job:
  - installs the built wheel into a clean venv;
  - dry-runs the CRD against the server;
  - runs `make schemas` and `git diff --exit-code schemas/`;
  - on failure, uploads the audit log, `kwokctl logs` and `kubectl get events -A`;
  - always deletes the cluster.
- `package` job: wheel, sdist, `SHA256SUMS`, CycloneDX SBOM, and `actions/attest-build-provenance`.

**Definition of done**
- [ ] The workflow is green on a pull request and on `main`.
- [ ] `actionlint` is clean.
- [ ] Full workflow wall time is under 10 minutes.
- [ ] Each negative check below fails the job it names.

**Tests that must pass**

| Check | Expected |
|---|---|
| Green run on a PR | All jobs pass |
| A PR breaking an example bundle | `manifests` fails |
| A PR with stale `schemas/` | `e2e` fails at `git diff --exit-code` |
| A PR adding `additionalProperties: false` beside `properties` in the CRD | `e2e` fails at the server dry run |
| A PR failing an e2e test | Diagnostics artifacts are uploaded; the cluster is deleted |

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
