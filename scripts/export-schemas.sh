#!/usr/bin/env bash
# Exports the cluster's OpenAPI v3 schemas for the kinds a bundle may contain, sorted and indented.
# Usage: export-schemas.sh [OUT_DIR]. OUT_DIR defaults to schemas/.
set -euo pipefail

mkdir -p "${1:-schemas}"
out=$(cd "${1:-schemas}" && pwd)
cd "$(dirname "$0")/.."

for path in api/v1 apis/apps/v1; do
  mkdir -p "$out/$(dirname "$path")"
  bin/kubectl --kubeconfig .deploysim/kubeconfig get --raw "/openapi/v3/$path" |
    python3 -c 'import json, sys; json.dump(json.load(sys.stdin), sys.stdout, indent=1, sort_keys=True); print()' \
      >"$out/$path.json"
done
