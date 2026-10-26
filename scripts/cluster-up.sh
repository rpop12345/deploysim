#!/usr/bin/env bash
# Creates the KWOK cluster, recreating it if stopped, and reconciles nodes, namespaces and the Release
# CRD; then checks that a canary pod becomes Ready.
# Usage: KWOK_VERSION=... KWOK_KUBE_VERSION=... cluster-up.sh. Writes .deploysim/kubeconfig.
set -euo pipefail

cd "$(dirname "$0")/.."
: "${KWOK_VERSION:?}" "${KWOK_KUBE_VERSION:?}"
export KWOK_KUBE_VERSION

name=${CLUSTER_NAME:-deploysim}
kubeconfig=.deploysim/kubeconfig
fingerprint_file=.deploysim/cluster.fingerprint

kubectl() {
  bin/kubectl --kubeconfig "$kubeconfig" "$@"
}

sha256() {
  if command -v sha256sum >/dev/null; then
    sha256sum | cut -d' ' -f1
  else
    shasum -a 256 | cut -d' ' -f1
  fi
}

create_cluster() {
  echo "cluster-up: creating cluster $name (Kubernetes $KWOK_KUBE_VERSION)" >&2
  bin/kwokctl create cluster --name "$name" --runtime binary --config kwok/stages.yaml \
    --kubeconfig "$kubeconfig" --kwok-controller-binary "$PWD/bin/kwok" --wait 120s -v WARN
  echo "$fingerprint" >"$fingerprint_file"
}

fingerprint=$({ echo "$KWOK_VERSION $KWOK_KUBE_VERSION"; cat kwok/stages.yaml; } | sha256)
mkdir -p .deploysim

if bin/kwokctl get clusters | grep -qx "$name"; then
  if [[ ! -f "$fingerprint_file" || "$(<"$fingerprint_file")" != "$fingerprint" ]]; then
    echo "cluster-up: cluster $name was created from other stages or versions; run make cluster-down cluster-up" >&2
    exit 1
  fi
  bin/kwokctl get kubeconfig --name "$name" >"$kubeconfig"
  if ! kubectl get --raw /readyz >/dev/null 2>&1; then
    echo "cluster-up: cluster $name is stopped; recreating it empty" >&2
    bin/kwokctl delete cluster --name "$name" --kubeconfig "$kubeconfig" -v WARN
    create_cluster
  fi
else
  create_cluster
fi

kubectl apply --server-side --field-manager=deploysim-setup -f kwok/cluster.yaml -f crds/release.yaml
kubectl wait --for condition=Established crd/releases.deploysim.io --timeout 30s
kubectl wait --for condition=Ready node --all --timeout 60s
for namespace in kube-system dev staging prod; do
  kubectl wait --for=create serviceaccount/default -n "$namespace" --timeout 60s >/dev/null
done

canary="deploysim-canary-$$"
kubectl run "$canary" -n kube-system --image=registry.k8s.io/pause:3.10 --restart=Never >/dev/null
if ! kubectl wait --for condition=Ready "pod/$canary" -n kube-system --timeout 30s >/dev/null; then
  kubectl delete pod "$canary" -n kube-system --wait=false >/dev/null
  echo "cluster-up: canary pod never became Ready; run make cluster-down cluster-up" >&2
  exit 1
fi
kubectl delete pod "$canary" -n kube-system --wait=false >/dev/null
echo "cluster-up: ready; kubeconfig at $kubeconfig" >&2
