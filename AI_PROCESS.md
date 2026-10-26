# AI-assisted development process

## Tools and models

- **Claude Code** (Anthropic's coding agent) in VS Code, running **Claude Opus 5.5**, for design,
  code, tests and running commands in the repository.
- Its web search and fetch tools and the `gh` CLI, for reading KWOK and kubectl-validate
  documentation and source, and for looking up pinned versions, checksums and action SHAs.

The agent ran in auto mode, with a classifier blocking destructive actions such as deleting git
branches. Commits, pushes and PRs happened only when I asked for them.

## What I asked for and how I iterated

1. **Specification first.** I had the assistant compare approaches, then write the README as the
   specification before any code. Its first design was a Python CLI with a local JSON state file.
   I changed direction to a real Kubernetes control plane: KWOK for the simulated environment and
   kubectl-validate for validation. It rewrote the specification after short experiments with
   both tools.
2. **Phased plan.** From the README I had it produce `docs/implementation-plan.md`: twelve phases,
   each with deliverables, a definition of done, and named tests that must pass.
3. **One phase at a time.** Each phase was built and verified, then delivered as one PR (P1–P11),
   which I merged after CI passed. For several phases I asked for manual test steps and ran some
   myself. When a phase turned up something the plan had not foreseen, the plan and README were
   updated in the same PR.

## Accepted, changed, rejected

**Accepted**:
- state kept in the cluster, in Release, bundle and state objects, not on the runner;
- a Lease for exclusion, plus compare-and-swap on `resourceVersion`;
- validation offline first, then against the cluster while holding the lock;
- rollback by re-applying the last known-good stored bundle, not `kubectl rollout undo`;
- crash recovery that always rolls back;
- faults injected through KWOK stages rather than mocks;
- end-to-end tests against a real control plane.

**Changed**, either by me or because verification contradicted the first attempt:
- **The whole design:** from a JSON state file to KWOK.
- **Drift detection:** `kubectl diff` reported false changes after an apply, and ignoring status
  was not enough. Replaced by a dry-run comparison made only when both reads have the same
  `resourceVersion`.
- **A stopped cluster:** restarting one proved unreliable, so it is now recreated.
- **The concurrency test:** it passed even without the lock it guarded, so it was rewritten until
  it failed without it.
- **The rollback target:** changed from `current` to `last_known_good`, to match the brief.
- **Two Linux-only bugs** found by the first CI run (a ServiceAccount race, and `fsync` on
  `/dev/null`) were fixed.
- **One misleading health message** was corrected.
- **Three failure-path gaps, fixed after the last phase:**
  - a kubectl timeout or an unexpected error exited 1, which means "rejected, nothing changed", and
    could leave the Lease held;
  - rollback left behind objects that only the failed release had added;
  - a deleted bundle ConfigMap made every recovery fail in the same way.

  Each now ends in a defined outcome, and has tests that fail without the fix.

**Rejected:**
- Bash, Go, and kind with Argo Rollouts as the implementation;
- the Docker runtime for KWOK;
- `kubectl rollout undo` and `kubectl diff`;
- an in-memory fake cluster;
- throwaway PRs to prove CI's failure paths, which I chose to prove locally instead.

## How the code was verified

- **Experiments** before relying on any tool's behaviour: KWOK stages, kubectl-validate's
  offline mode and CEL support, Lease semantics, kubectl error messages.
- **185 tests**, 117 unit and 68 end-to-end against a real API server on KWOK, with 96% coverage
  against an 85% gate.
- **Tests checked to fail:** each key test was shown to fail when its feature was broken, for
  example the file lock removed, Lease renewal disabled, or kubeconfig isolation bypassed.
- **Stress and repeat runs** to rule out flaky tests.
- **From scratch:** the demo and the README walkthrough, run verbatim on fresh throwaway clusters.
- **`make demo`:** 23 scenarios covering every exit code.
- **CI:** lint and type checks, unit tests, end-to-end tests and the demo against the built wheel.
  Build provenance was verified with `gh attestation verify`.
- **README checks:** a test fails if the README's exit codes, commands or faults drift from the
  code.

## Limitations and what I would not ship as is

It is a simulator: pods never run, so verification proves rollout mechanics, not application
health. Namespaces stand in for separate clusters.

Before shipping, I would want:
- `controller.py` (about 900 lines, largely generated) reviewed line by line, and unit-tested
  with a fake cluster rather than relying mostly on end-to-end coverage;
- the fault-injection flags, including `os._exit`, moved out of the shipped CLI;
- kubectl error handling based on typed API errors, not regexes over stderr;
- crash testing at arbitrary points, not just three injected ones;
- Lease renewal throughout a run, not only while waiting on a rollout.

Risks:
- the audit log is tamper-evident but local;
- the bundle policy runs only client-side, where admission control belongs;
- kwokctl's control-plane binaries are not checksum-pinned;
- objects removed from a bundle are not pruned on forward deploys.

Safety rests on the kubeconfig guard. Every tool here is pinned to the simulator's kubeconfig and refuses any non-loopback server.

Design reasoning is in the README (Design decisions, Retry and recovery, Limitations) and, phase by
phase, in `docs/implementation-plan.md`.
