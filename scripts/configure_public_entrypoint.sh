#!/usr/bin/env bash
# Install the host-only public reverse proxy without exposing controller,
# P4Runtime, or backend ports.  This script intentionally does not touch
# Docker volumes, databases, policies, or the watcher profile.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

[[ -f .env ]] || { echo ".env is required." >&2; exit 1; }
command -v nginx >/dev/null 2>&1 || { echo "nginx must be installed first." >&2; exit 1; }

python3 - "$ROOT_DIR/deploy/nginx/idn-public.conf.template" /etc/nginx/sites-available/idn-public <<'PY'
import os
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
values = {}
for raw in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

key = values.get("IDN_API_KEY", "")
if not key or "\n" in key or "\r" in key:
    raise SystemExit("IDN_API_KEY is required and must be one line.")
public_name = values.get("IDN_PUBLIC_SERVER_NAME", "_") or "_"
if any(c in public_name for c in "\r\n"):
    raise SystemExit("IDN_PUBLIC_SERVER_NAME must be one line.")

def quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

rendered = template.replace("__IDN_API_KEY__", quoted(key)).replace(
    "__IDN_PUBLIC_SERVER_NAME__", public_name
)
Path(sys.argv[2]).write_text(rendered, encoding="utf-8")
PY

ln -sfn /etc/nginx/sites-available/idn-public /etc/nginx/sites-enabled/idn-public
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
# `enable --now` does not reload an already-running Nginx service after a
# package install.  Activate the freshly rendered virtual host without
# interrupting existing HTTP connections.
systemctl reload nginx

# Older running controller containers may still carry host-published ONOS
# diagnostics ports until their next controlled maintenance recreate.  Guard
# all non-public Docker TCP ports at the host boundary now, without stopping
# ONOS or Mininet.  The Compose loopback bindings remain the durable desired
# state for the next recreate.
install -d -m 0755 /usr/local/sbin
cat >/usr/local/sbin/idn-public-port-guard <<'SH'
#!/usr/bin/env bash
set -euo pipefail
interface="$(ip route show default | awk 'NR==1 {print $5}')"
[[ -n "$interface" ]] || exit 0
for tool in iptables ip6tables; do
  command -v "$tool" >/dev/null 2>&1 || continue
  "$tool" -N DOCKER-USER 2>/dev/null || true
  for port in 5173 8000 8181 8101 6653; do
    "$tool" -C DOCKER-USER -i "$interface" -p tcp --dport "$port" -j DROP 2>/dev/null || \
      "$tool" -I DOCKER-USER -i "$interface" -p tcp --dport "$port" -j DROP
  done
done
SH
chmod 0750 /usr/local/sbin/idn-public-port-guard
cat >/etc/systemd/system/idn-public-port-guard.service <<'UNIT'
[Unit]
Description=Block direct public access to IDN Docker control ports
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/idn-public-port-guard
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now idn-public-port-guard
echo "Public entry point configured: 80 -> frontend, /api/ -> backend."
