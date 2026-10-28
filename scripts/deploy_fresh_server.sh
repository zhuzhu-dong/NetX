#!/usr/bin/env bash
# One-command fresh installation for a clean Linux server.
# No existing database, stable capacity record, Policy, Binding or P4 artifact
# on the host is required.  Secrets are checked but never printed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SKIP_BOOTSTRAP=false
SMOKE_TEST=true
SKIP_BUILD=false
for option in "$@"; do
  case "$option" in
    --skip-bootstrap) SKIP_BOOTSTRAP=true ;;
    --no-smoke-test) SMOKE_TEST=false ;;
    --skip-build) SKIP_BUILD=true ;;
    *) echo "usage: $0 [--skip-bootstrap] [--no-smoke-test] [--skip-build]" >&2; exit 2 ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required." >&2; exit 1; }
[[ -f .env ]] || { echo ".env is required; copy .env.example and set secrets." >&2; exit 1; }

has_env_value() {
  local key="$1"
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=[^[:space:]#]+" .env
}

has_env_value IDN_API_KEY || { echo "Required secret IDN_API_KEY is EMPTY." >&2; exit 1; }
if ! has_env_value WANGTIAO2026_API_KEY && ! has_env_value DASHSCOPE_API_KEY && ! has_env_value OPENAI_API_KEY; then
  echo "A real LLM API key is required (WANGTIAO2026_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY is EMPTY)." >&2
  exit 1
fi
if grep -Eq "^[[:space:]]*IDN_MOCK_MODE[[:space:]]*=[[:space:]]*true([[:space:]#]|$)" .env; then
  echo "IDN_MOCK_MODE must be false for a production fresh install." >&2
  exit 1
fi
echo "Required secrets: IDN_API_KEY=SET, LLM_API_KEY=SET"

if ! docker network inspect idn-net >/dev/null 2>&1; then
  docker network create idn-net >/dev/null
fi

infra=(docker compose -f docker-compose.infra.yml)
app=(docker compose -f docker-compose.infra.yml -f docker-compose.app.yml -f docker-compose.prod.yml)
build_args=(--build)
if [[ "$SKIP_BUILD" == true ]]; then
  # Upgrade mode: production images were loaded from a verified local build.
  # Do not force ECS to pull base images or rebuild on the server.
  build_args=()
fi

wait_healthy() {
  local container="$1" attempts="${2:-60}" status=""
  for ((i=1; i<=attempts; i++)); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && return 0
    sleep 2
  done
  echo "Container $container did not become healthy (last status: ${status:-missing})." >&2
  return 1
}

"${infra[@]}" up -d "${build_args[@]}" onos
wait_healthy idn-onos 100
"${infra[@]}" up -d "${build_args[@]}" mininet
wait_healthy idn-mininet 60
"${app[@]}" up -d "${build_args[@]}" idn-system idn-frontend
wait_healthy idn-backend 60
wait_healthy idn-frontend 60

# The production overlay leaves image-copied P4 artifacts visible even when a
# fresh host's p4/build directory is absent or empty.
"${app[@]}" exec -T idn-system sh -ec '
  test -s /app/p4/build/idn_basic.json
  test -s /app/p4/build/idn_basic.p4info.txt
  test -s /app/p4/build/p4info.env
  test -s "/app/mininet/topologies/${TOPO_NAME:-research_dc}.yaml"
'
"${infra[@]}" exec -T mininet sh -ec '
  test -s /app/p4/build/idn_basic.json
  test -s /app/p4/build/idn_basic.p4info.txt
  test -s /app/p4/build/p4info.env
  python3 - <<"PY"
import socket
for port in range(50051, 50057):
    with socket.create_connection(("127.0.0.1", port), timeout=3):
        pass
PY
'

if [[ "$SKIP_BOOTSTRAP" == false ]]; then
  # The credential remains inside the backend container.  It is never read
  # into the deploy shell or printed to logs.
  "${app[@]}" exec -T idn-system python - <<'PY'
import json
import os
import urllib.request

key = os.environ.get("IDN_API_KEY", "")
assert key, "IDN_API_KEY is EMPTY"
payload = json.dumps({"source": "fresh-install"}).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/network/bootstrap",
    data=payload,
    headers={"Content-Type": "application/json", "X-API-Key": key},
    method="POST",
)
urllib.request.urlopen(request, timeout=60).read()
PY
fi

"${app[@]}" exec -T idn-system python - <<'PY'
import json
import urllib.request

ready = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health/ready", timeout=10))
assert ready["status"] == "healthy", ready
onos = ready["components"]["onos"]
assert onos["device_count"] == 6 and onos["available_devices"] == 6, onos
print("backend=healthy onos_devices=6 available_devices=6")
PY

if [[ "$SMOKE_TEST" == true ]]; then
  "${infra[@]}" exec -T mininet python3 - <<'PY'
import json
import urllib.parse
import urllib.request

topology = json.load(urllib.request.urlopen("http://127.0.0.1:9001/topology", timeout=10))
assert len(topology.get("switches") or []) == 6, topology
assert len(topology.get("hosts") or []) == 5, topology
assert len(topology.get("links") or []) == 13, topology
query = urllib.parse.urlencode({"src": "h_research", "dst": "h_dc", "count": 3})
ping = json.load(urllib.request.urlopen("http://127.0.0.1:9001/probe/ping?" + query, timeout=15))
assert ping.get("status") == "ok" and float(ping.get("loss_rate") or 0) == 0, ping
print("mininet_switches=6 hosts=5 links=13 research_to_dc_loss=0")
PY
fi

echo "Deployment completed. Existing persistent data was preserved; on a fresh server, the first business Intent triggers formal candidate calibration."
