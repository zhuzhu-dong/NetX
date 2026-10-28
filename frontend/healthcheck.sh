#!/bin/sh
# Docker healthcheck for the immutable static frontend.  Keep this as a file
# rather than an inline Compose string so Docker/Compose/shell escaping cannot
# weaken the production contract.
set -eu

index_file="$(mktemp)"
trap 'rm -f "$index_file"' EXIT

wget -q -O "$index_file" http://127.0.0.1:5173/
grep -q '/assets/' "$index_file"
! grep -qE '/@vite/client|/src/main\.js' "$index_file"

assets="$(grep -oE '/assets/[^"?]+\.(js|css)' "$index_file" | sort -u)"
test -n "$assets"

for asset in $assets; do
  wget -q -O /dev/null "http://127.0.0.1:5173$asset"
done
