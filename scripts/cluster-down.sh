#!/usr/bin/env bash
# Deletes the KWOK cluster and its kubeconfig. Safe to run when no cluster exists.
set -euo pipefail

cd "$(dirname "$0")/.."
name=${CLUSTER_NAME:-deploysim}
kubeconfig=.deploysim/kubeconfig

if bin/kwokctl get clusters | grep -qx "$name"; then
  bin/kwokctl delete cluster --name "$name" --kubeconfig "$kubeconfig" -v WARN
  echo "cluster-down: cluster $name deleted" >&2
else
  echo "cluster-down: no cluster $name" >&2
fi
rm -f "$kubeconfig" .deploysim/cluster.fingerprint
