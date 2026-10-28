from __future__ import annotations

from pathlib import Path


def _repository_root() -> Path:
    """Locate compose sources in both host and container test layouts."""
    for candidate in (Path(__file__).resolve().parents[2], Path("/workspace")):
        if (candidate / "docker" / "Dockerfile").is_file():
            return candidate
    raise FileNotFoundError("repository root with docker/Dockerfile is unavailable")


def test_production_backend_image_carries_p4_and_resource_topology_inputs():
    """Fresh production startup cannot depend on development bind mounts."""
    root = _repository_root()
    dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    deploy = (root / "scripts" / "deploy_fresh_server.sh").read_text(encoding="utf-8")

    assert "COPY p4/build/ /app/p4/build/" in dockerfile
    assert "COPY mininet/topologies/ /app/mininet/topologies/" in dockerfile
    assert 'test -s "/app/mininet/topologies/${TOPO_NAME:-research_dc}.yaml"' in deploy


def test_production_overlay_removes_only_backend_development_mounts():
    root = _repository_root()
    overlay = (root / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "volumes: !override" in overlay
    assert "- idn-app-data:/app/data" in overlay
    assert "./p4/build:/app/p4/build" not in overlay


def test_deploy_script_does_not_claim_an_upgrade_database_is_empty():
    """The same deploy entry point supports fresh installs and data-preserving upgrades."""
    root = _repository_root()
    deploy = (root / "scripts" / "deploy_fresh_server.sh").read_text(encoding="utf-8")

    assert "Existing persistent data was preserved" in deploy
    assert "Database remains empty" not in deploy
