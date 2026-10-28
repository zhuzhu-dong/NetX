#!/bin/sh
# Verify an already-running immutable frontend container through HTTP.  The
# script is intentionally read-only; it does not install packages or mutate a
# runtime container.  Usage: verify_frontend_production.sh [base_url] [intent]
set -eu

base_url="${1:-http://127.0.0.1:5173}"
intent_id="${2:-INT-20260809-098fdae4}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

curl -fsS --retry 2 --connect-timeout 5 "$base_url/" -o "$tmp_dir/index.html"

if ! grep -q '/assets/' "$tmp_dir/index.html"; then
  echo "FAIL: index has no built /assets/ references" >&2
  exit 1
fi
if grep -qE '/@vite/client|/src/main\.js' "$tmp_dir/index.html"; then
  echo "FAIL: index still references Vite development entry points" >&2
  exit 1
fi

# Fetch every JS/CSS asset referenced by the production HTML shell.
assets="$(grep -Eo '(src|href)="/assets/[^"?]+\.(js|css)"' "$tmp_dir/index.html" | sed -E 's/^[a-z]+="//; s/"$//' | sort -u)"
if [ -z "$assets" ]; then
  echo "FAIL: no production JavaScript or CSS assets found" >&2
  exit 1
fi
for asset in $assets; do
  curl -fsS --retry 2 --connect-timeout 5 "$base_url$asset" -o /dev/null
done

# HTTP hard-refresh equivalent: each request bypasses caches and must serve
# the production shell.  The specified deep links also prove SPA fallback.
for attempt in $(seq 1 10); do
  curl -fsS --retry 2 --connect-timeout 5 -H 'Cache-Control: no-cache' "$base_url/dashboard" -o "$tmp_dir/dashboard-$attempt.html"
  grep -q '/assets/' "$tmp_dir/dashboard-$attempt.html"
  ! grep -qE '/@vite/client|/src/main\.js' "$tmp_dir/dashboard-$attempt.html"
done
for route in \
  "/dashboard/intents/$intent_id/detail" \
  "/dashboard/evidence/$intent_id" \
  "/dashboard/decision-report" \
  "/dashboard/topology" \
  "/dashboard/runtime"; do
  curl -fsS --retry 2 --connect-timeout 5 "$base_url$route" -o "$tmp_dir/route.html"
  grep -q '/assets/' "$tmp_dir/route.html"
done

echo "PASS: production assets, 10 no-cache dashboard refreshes, and SPA deep links verified at $base_url"
