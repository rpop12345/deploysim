#!/usr/bin/env bash
# Runs every deploysim scenario against the KWOK cluster and checks each exit code.
# Usage: scripts/demo.sh after make cluster-up; needs jq. DEPLOYSIM overrides the CLI binary,
# AUDIT_LOG the audit log path; KEEP=1 keeps the demo app and audit log.
set -euo pipefail

cd "$(dirname "$0")/.."
deploysim=${DEPLOYSIM:-.venv/bin/deploysim}
app="demo-$(date +%s)"
work=$(mktemp -d)
audit=${AUDIT_LOG:-$work/audit.jsonl}
scenarios=0
failures=0

kubectl() {
  bin/kubectl --kubeconfig .deploysim/kubeconfig "$@"
}

cleanup() {
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "kept app $app; audit log $audit"
    return
  fi
  for env in dev staging prod; do
    kubectl delete deployments,services,configmaps,releases.deploysim.io,leases -n "$env" \
      -l "app.kubernetes.io/name=$app" --ignore-not-found --wait=false >/dev/null
  done
  rm -rf "$work"
}
trap cleanup EXIT

for version in 1.0.0 1.1.0 1.2.0; do
  sed -e "s/orders-api/$app/g" \
    -e "s/progressDeadlineSeconds: 45/progressDeadlineSeconds: 10/" \
    -e "s/timeoutSeconds: 60/timeoutSeconds: 20/" \
    "examples/orders-api-$version.yaml" >"$work/$version.yaml"
done

cat >"$work/remote.kubeconfig" <<'EOF'
apiVersion: v1
kind: Config
clusters: [{name: prod, cluster: {server: "https://prod.example.com:6443"}}]
users: [{name: admin, user: {}}]
contexts: [{name: prod, context: {cluster: prod, user: admin}}]
current-context: prod
EOF

# expect CODE DESCRIPTION COMMAND...: runs COMMAND and checks it exits CODE.
expect() {
  local want=$1 description=$2 code=0 outcome
  shift 2
  scenarios=$((scenarios + 1))
  "$@" >"$work/out" 2>"$work/err" || code=$?
  outcome=$(jq -r '.outcome // .result // (if has("verified") then "verified=\(.verified)" else empty end)' \
    "$work/out" 2>/dev/null || true)
  if [[ "$code" == "$want" ]]; then
    printf '  ok    exit %-3s %-20s %s\n' "$code" "$outcome" "$description"
  else
    printf '  FAIL  exit %-3s expected %-3s %s\n' "$code" "$want" "$description"
    tail -5 "$work/err" | sed 's/^/        /'
    failures=$((failures + 1))
  fi
}

run() {
  "$deploysim" "$@" --audit-log "$audit"
}

v1="$work/1.0.0.yaml"
v2="$work/1.1.0.yaml"
broken="$work/1.2.0.yaml"

echo "deploysim demo: app $app"
echo
echo "Validation and safety"
expect 0 "valid bundle passes offline validation" run validate "$v1"
expect 1 "unknown field is rejected" run validate tests/fixtures/invalid/unknown-field.yaml
expect 64 "missing --env is a usage error" "$deploysim" deploy "$v1"
expect 5 "a real cluster's kubeconfig is refused" \
  run deploy "$v1" --env dev --kubeconfig "$work/remote.kubeconfig"

echo "Deploy, promote, repeat"
expect 0 "1.0.0 to dev" run deploy "$v1" --env dev
expect 0 "1.0.0 to dev again changes nothing" run deploy "$v1" --env dev
expect 0 "1.0.0 to staging" run deploy "$v1" --env staging
expect 1 "1.1.0 to staging before dev: promotion gate" run deploy "$v2" --env staging
expect 0 "1.1.0 to dev" run deploy "$v2" --env dev

echo "Automatic rollback"
expect 2 "1.1.0 to staging, unhealthy: back to 1.0.0" \
  run deploy "$v2" --env staging --inject-fault unhealthy
expect 2 "1.2.0 (broken build) to dev: back to 1.1.0" run deploy "$broken" --env dev

echo "Crash recovery"
expect 70 "1.1.0 to staging crashes after DEPLOY" \
  run deploy "$v2" --env staging --inject-fault crash-after-deploy --lease-seconds 5
expect 4 "deploy while the dead run's lease is live" run deploy "$v2" --env staging
sleep 6
expect 0 "deploy after expiry: recovers, then succeeds" run deploy "$v2" --env staging
expect 70 "1.1.0 to prod crashes after PREPARE" \
  run deploy "$v2" --env prod --inject-fault crash-after-prepare --lease-seconds 5
sleep 6
expect 0 "recover command: prod back to EMPTY" run recover --app "$app" --env prod
expect 0 "1.1.0 to prod" run deploy "$v2" --env prod

echo "Failed rollback and the degraded gate"
expect 3 "1.0.0 to dev, rollback also unhealthy" \
  run deploy "$v1" --env dev --inject-fault unhealthy-rollback
expect 1 "next deploy into DEGRADED dev is rejected" run deploy "$v2" --env dev
expect 0 "--allow-degraded fixes forward" run deploy "$v2" --env dev --allow-degraded

echo "Drift"
kubectl scale deployment "$app" -n dev --replicas=1 >/dev/null
expect 0 "manual kubectl scale is corrected" run deploy "$v2" --env dev

echo "Inspection"
expect 0 "status" run status --app "$app" --env dev
expect 0 "audit log verifies" run history --verify
echo
kubectl get releases.deploysim.io -A -l "app.kubernetes.io/name=$app"
echo
if ((failures)); then
  echo "$failures of $scenarios scenarios did not exit as expected"
  exit 1
fi
echo "all $scenarios scenarios exited as expected"
