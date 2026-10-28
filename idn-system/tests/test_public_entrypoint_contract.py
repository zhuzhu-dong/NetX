from __future__ import annotations

from pathlib import Path


def _repository_root() -> Path:
    for candidate in (Path(__file__).resolve().parents[2], Path("/workspace")):
        if (candidate / "docker-compose.app.yml").is_file():
            return candidate
    raise FileNotFoundError("repository root unavailable")


def test_public_entrypoint_keeps_control_and_api_ports_on_loopback():
    root = _repository_root()
    app = (root / "docker-compose.app.yml").read_text(encoding="utf-8")
    infra = (root / "docker-compose.infra.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:8000:8000"' in app
    assert '"127.0.0.1:5173:5173"' in app
    for port in ("8181", "8101", "6653"):
        assert f'"127.0.0.1:{port}:{port}"' in infra


def test_public_entrypoint_uses_server_side_api_auth_not_a_browser_fallback():
    root = _repository_root()
    client = (root / "frontend" / "src" / "api" / "client.js").read_text(encoding="utf-8")
    vite = (root / "frontend" / "vite.config.js").read_text(encoding="utf-8")
    nginx = (root / "deploy" / "nginx" / "idn-public.conf.template").read_text(encoding="utf-8")

    assert "demo-key-xxx" not in client
    assert "IDN_PROXY_API_KEY" in vite
    assert 'proxy_set_header X-API-Key "__IDN_API_KEY__";' in nginx


def test_public_entrypoint_installs_a_persistent_control_port_guard():
    root = _repository_root()
    script = (root / "scripts" / "configure_public_entrypoint.sh").read_text(encoding="utf-8")

    assert "idn-public-port-guard.service" in script
    assert "DOCKER-USER" in script
    assert "for port in 5173 8000 8181 8101 6653" in script


def test_public_entrypoint_reloads_an_already_running_nginx_service():
    root = _repository_root()
    script = (root / "scripts" / "configure_public_entrypoint.sh").read_text(encoding="utf-8")

    assert "systemctl enable --now nginx" in script
    assert "systemctl reload nginx" in script
