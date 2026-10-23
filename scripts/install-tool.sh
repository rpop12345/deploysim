#!/usr/bin/env bash
# Installs bin/NAME from URL after verifying its SHA-256; skips if bin/.stamps/NAME holds that SHA-256.
# Usage: install-tool.sh NAME URL SHA256 [TAR_MEMBER]. BIN_DIR overrides bin/.
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 NAME URL SHA256 [TAR_MEMBER]" >&2
  exit 64
fi

name=$1
url=$2
want=$3
member=${4:-}
bin_dir=${BIN_DIR:-bin}
target="$bin_dir/$name"
stamp="$bin_dir/.stamps/$name"

if [[ -z "$want" ]]; then
  echo "install-tool: no pinned SHA-256 for $name on this platform" >&2
  exit 1
fi

if [[ -x "$target" && -f "$stamp" && "$(<"$stamp")" == "$want" ]]; then
  exit 0
fi

sha256() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

mkdir -p "$bin_dir/.stamps"
rm -f "$target" "$stamp"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "install-tool: $name <- $url" >&2
curl -fsSL --retry 3 -o "$tmp/artifact" "$url"

got=$(sha256 "$tmp/artifact")
if [[ "$got" != "$want" ]]; then
  echo "install-tool: SHA-256 mismatch for $name: want $want, got $got" >&2
  exit 1
fi

if [[ -n "$member" ]]; then
  tar -xzf "$tmp/artifact" -C "$tmp" "$member"
  mv "$tmp/$member" "$target"
else
  mv "$tmp/artifact" "$target"
fi
chmod 0755 "$target"
echo "$want" >"$stamp"
