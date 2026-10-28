#!/usr/bin/env bash
# Read-only Compose stack diagnostics.  It discovers containers from service
# metadata and never assumes generated container names.
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
INFRA=(docker compose -f docker-compose.infra.yml)
APP=(docker compose -f docker-compose.app.yml)

section() { echo; echo "== $1 =="; }
http_status() {
  local label="$1" url="$2" auth="${3:-}"
  local args=(-sS -o /dev/null -w '%{http_code}' --max-time 8)
  [[ -n "$auth" ]] && args+=(-u "$auth")
  local code
  code="$(curl "${args[@]}" "$url" 2>/dev/null || true)"
  printf '%-24s %s\n' "$label" "${code:-unreachable}"
}

section "Compose services"
printf 'infra: '; "${INFRA[@]}" config --services | paste -sd, -
printf 'app:   '; "${APP[@]}" config --services | paste -sd, -

section "Runtime containers"
"${INFRA[@]}" ps --format json
"${APP[@]}" ps --format json

section "Health and restart counters"
for stack in INFRA APP; do
  declare -n compose_ref="$stack"
  while IFS= read -r service; do
    cid="$("${compose_ref[@]}" ps -q "$service" 2>/dev/null || true)"
    if [[ -z "$cid" ]]; then
      printf '%-18s %s\n' "$service" "not-created"
      continue
    fi
    docker inspect --format '{{.Name}} service='"$service"' status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} ports={{json .NetworkSettings.Ports}}' "$cid"
  done < <("${compose_ref[@]}" config --services)
done

section "HTTP readiness"
http_status "ONOS devices" "http://127.0.0.1:8181/onos/v1/devices" "onos:rocks"
http_status "Backend readiness" "http://127.0.0.1:8000/api/v1/health/ready"
http_status "Frontend root" "http://127.0.0.1:5173/"

section "Mininet probe"
"${INFRA[@]}" exec -T mininet python3 -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:9001/topology',timeout=8)); print(json.dumps({'status':d.get('status','ok'),'devices':len(d.get('switches',[])),'links':len(d.get('links',[])),'hosts':len(d.get('hosts',[]))},ensure_ascii=False))" 2>/dev/null || echo "mininet probe unavailable"

section "Backend topology"
curl -fsS --max-time 8 "http://127.0.0.1:8000/api/v1/topology/current" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); s=d.get('topology_summary') or {}; print(json.dumps({'devices':s.get('devices',len(d.get('devices',[]))),'directed_links':s.get('directed_device_links'),'physical_links':s.get('physical_device_links'),'hosts':s.get('hosts',len(d.get('hosts',[]))),'path_config':d.get('path_config')},ensure_ascii=False))" 2>/dev/null || echo "backend topology unavailable"

section "Active policies (read-only SQLite)"
"${APP[@]}" exec -T idn-system python -c "import json,sqlite3; c=sqlite3.connect('file:/app/data/app.db?mode=ro',uri=True); c.row_factory=sqlite3.Row; rows=c.execute(\"SELECT policy_id,intent_id,traffic_scope_key,status,version,pending_intent_id,updated_at FROM policies WHERE status='ACTIVE' ORDER BY updated_at DESC\").fetchall(); print(json.dumps([dict(r) for r in rows],ensure_ascii=False)); c.close()" 2>/dev/null || echo "active policy evidence unavailable"

section "Most recent failed intent"
curl -fsS --max-time 8 "http://127.0.0.1:8000/api/v1/intents?status=failed&page=1&page_size=1" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); rows=d.get('items',d if isinstance(d,list) else []); r=rows[0] if rows else {}; result=r.get('result') or {}; decision=result.get('consensus_decision') or {}; print(json.dumps({'intent_id':r.get('intent_id') or r.get('id'),'status':r.get('status'),'reason_code':decision.get('reason_code'),'reason_detail':decision.get('reason_detail')},ensure_ascii=False))" 2>/dev/null || echo "failed intent evidence unavailable"
