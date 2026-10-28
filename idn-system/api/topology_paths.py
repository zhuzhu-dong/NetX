"""Validated configured path templates for the research_dc topology.

The JSON file is desired-state metadata only. Runtime selection still uses
the live Mininet/ONOS graph; this registry validates and names the selected
device sequence so reports never confuse a configured template with reality.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topology_paths.json"

PATH_CONFIG_INVALID = "PATH_CONFIG_INVALID"
PATH_DEVICE_MISSING = "PATH_DEVICE_MISSING"
PATH_LINK_MISSING = "PATH_LINK_MISSING"
PATH_ENDPOINT_MISMATCH = "PATH_ENDPOINT_MISMATCH"
BACKUP_PATH_IDENTICAL = "BACKUP_PATH_IDENTICAL"
QOS_EGRESS_NOT_ON_PATH = "QOS_EGRESS_NOT_ON_PATH"


@dataclass(frozen=True)
class PathValidationError:
    code: str
    path_id: str | None
    detail: str


@dataclass(frozen=True)
class BottleneckCheckpoint:
    switch_name: str
    next_node: str


@dataclass(frozen=True)
class ExpectedPathLink:
    link_id: str
    from_device: str
    to_device: str
    from_port: int
    to_port: int
    capacity_mbps: int

    @property
    def device_pair(self) -> tuple[str, str]:
        return (
            min(self.from_device, self.to_device),
            max(self.from_device, self.to_device),
        )


@dataclass(frozen=True)
class QosEgressCandidate:
    device_id: str
    switch: str
    peer_device_id: str
    peer_switch: str
    egress_port: int
    interface: str
    link_id: str
    capacity_mbps: int


@dataclass(frozen=True)
class TopologyPath:
    path_id: str
    name: str
    src_host: str
    dst_host: str
    role: str
    devices: list[str]
    switches: list[str]
    expected_links: list[ExpectedPathLink]
    qos_egress_candidates: list[QosEgressCandidate]
    path_capacity_mbps: int
    bottleneck_link_id: str
    enabled: bool = True
    description: str = ""
    service_id: str = ""

    @property
    def switch_path(self) -> list[str]:
        return list(self.switches)

    @property
    def bottleneck_links(self) -> set[str]:
        return {self.bottleneck_link_id} if self.bottleneck_link_id else set()

    @property
    def bottleneck_checkpoints(self) -> list[BottleneckCheckpoint]:
        for link in self.expected_links:
            if link.link_id == self.bottleneck_link_id:
                index = self.devices.index(link.from_device)
                return [
                    BottleneckCheckpoint(self.switches[index], self.switches[index + 1])
                ]
        return []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TopologyPathRegistry:
    """Load, validate, match and select named path templates."""

    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = str(
            config_path or os.getenv("TOPOLOGY_PATHS_FILE") or _CONFIG_PATH
        )
        self.schema_version = ""
        self.topology_name = ""
        self.device_switch_map: dict[str, str] = {}
        self.host_attachment_map: dict[str, dict[str, Any]] = {}
        self.physical_links: list[dict[str, Any]] = []
        self._paths: dict[str, TopologyPath] = {}
        self.errors: list[PathValidationError] = []
        self._load()

    @property
    def valid(self) -> bool:
        return not self.errors and bool(self._paths)

    def get(self, name: str) -> TopologyPath | None:
        exact = self._paths.get(name)
        if exact is not None:
            return exact
        role = {"primary": "PRIMARY", "secondary": "BACKUP", "backup": "BACKUP"}.get(
            name.lower()
        )
        return self.get_by_role(role) if role else None

    def get_by_role(
        self,
        role: str | None,
        *,
        src_host: str | None = None,
        dst_host: str | None = None,
    ) -> TopologyPath | None:
        if role is None:
            return None
        candidates = [
            path
            for path in self._paths.values()
            if path.role == role.upper()
            and (src_host is None or path.src_host == src_host)
            and (dst_host is None or path.dst_host == dst_host)
            and path.enabled
        ]
        candidates.sort(key=lambda item: (item.src_host, item.dst_host, item.path_id))
        return candidates[0] if candidates else None

    def items(self):  # noqa: ANN201
        return self._paths.items()

    def primary_switch_path(self) -> list[str]:
        path = self.get("primary")
        return path.switch_path if path else []

    def secondary_switch_path(self) -> list[str]:
        path = self.get("secondary")
        return path.switch_path if path else []

    def primary_bottleneck_links(self) -> set[str]:
        path = self.get("primary")
        return path.bottleneck_links if path else set()

    def secondary_bottleneck_links(self) -> set[str]:
        path = self.get("secondary")
        return path.bottleneck_links if path else set()

    def primary_bottleneck_checkpoints(self) -> list[BottleneckCheckpoint]:
        path = self.get("primary")
        return path.bottleneck_checkpoints if path else []

    def secondary_bottleneck_checkpoints(self) -> list[BottleneckCheckpoint]:
        path = self.get("secondary")
        return path.bottleneck_checkpoints if path else []

    def match_path(
        self,
        *,
        switches: Iterable[str] | None = None,
        devices: Iterable[str] | None = None,
        src_host: str | None = None,
        dst_host: str | None = None,
    ) -> TopologyPath | None:
        switch_list = list(switches or [])
        device_list = list(devices or [])
        for path in self._paths.values():
            if not path.enabled:
                continue
            if src_host and path.src_host != src_host:
                continue
            if dst_host and path.dst_host != dst_host:
                continue
            if switch_list and path.switches != switch_list:
                continue
            if device_list and path.devices != device_list:
                continue
            return path
        return None

    def validate_runtime(self, topology: dict[str, Any] | None) -> dict[str, Any]:
        topology = topology or {}
        switches = topology.get("switches") or []
        hosts = topology.get("hosts") or []
        links = topology.get("links") or []
        runtime_devices = {
            str(item.get("device_id")): str(item.get("name"))
            for item in switches
            if item.get("device_id") and item.get("name")
        }
        runtime_hosts = {str(item.get("name")) for item in hosts if item.get("name")}
        runtime_pairs: set[tuple[str, str]] = set()
        active_pairs: set[tuple[str, str]] = set()
        runtime_ports: dict[tuple[str, str], dict[str, int]] = {}
        for link in links:
            endpoints = link.get("endpoints") or []
            switch_endpoints = [
                item
                for item in endpoints
                if item.get("type") == "switch" and item.get("device_id")
            ]
            switch_devices = [str(item.get("device_id")) for item in switch_endpoints]
            if len(switch_devices) != 2:
                continue
            pair: tuple[str, str] = (
                min(switch_devices[0], switch_devices[1]),
                max(switch_devices[0], switch_devices[1]),
            )
            runtime_pairs.add(pair)
            if all(item.get("port") is not None for item in switch_endpoints):
                runtime_ports[pair] = {
                    str(item["device_id"]): int(item["port"])
                    for item in switch_endpoints
                }
            if str(link.get("state", "ACTIVE")).upper() == "ACTIVE":
                active_pairs.add(pair)

        errors = list(self.errors)
        available: list[str] = []
        unavailable: list[str] = []
        path_reports: list[dict[str, Any]] = []
        for path in self._paths.values():
            path_errors: list[PathValidationError] = []
            for device, switch in zip(path.devices, path.switches):
                if device not in runtime_devices:
                    path_errors.append(
                        PathValidationError(
                            PATH_DEVICE_MISSING,
                            path.path_id,
                            f"missing runtime device {device}",
                        )
                    )
                elif runtime_devices[device] != switch:
                    path_errors.append(
                        PathValidationError(
                            PATH_CONFIG_INVALID,
                            path.path_id,
                            f"{device} maps to {runtime_devices[device]}, expected {switch}",
                        )
                    )
            if path.src_host not in runtime_hosts or path.dst_host not in runtime_hosts:
                path_errors.append(
                    PathValidationError(
                        PATH_ENDPOINT_MISMATCH,
                        path.path_id,
                        f"runtime endpoints {path.src_host}->{path.dst_host} are unavailable",
                    )
                )
            for link in path.expected_links:
                if link.device_pair not in runtime_pairs:
                    path_errors.append(
                        PathValidationError(
                            PATH_LINK_MISSING,
                            path.path_id,
                            f"missing runtime link {link.link_id}",
                        )
                    )
                elif link.device_pair not in active_pairs:
                    path_errors.append(
                        PathValidationError(
                            PATH_LINK_MISSING,
                            path.path_id,
                            f"inactive runtime link {link.link_id}",
                        )
                    )
                elif link.device_pair in runtime_ports:
                    ports = runtime_ports[link.device_pair]
                    if (
                        ports.get(link.from_device) != link.from_port
                        or ports.get(link.to_device) != link.to_port
                    ):
                        path_errors.append(
                            PathValidationError(
                                PATH_LINK_MISSING,
                                path.path_id,
                                f"runtime ports for {link.link_id} differ from configured {link.from_port}->{link.to_port}",
                            )
                        )
            errors.extend(path_errors)
            if path_errors or not path.enabled:
                unavailable.append(path.path_id)
            else:
                available.append(path.path_id)
            path_reports.append(
                {
                    "path_id": path.path_id,
                    "service_id": path.service_id,
                    "name": path.name,
                    "role": path.role,
                    "src_host": path.src_host,
                    "dst_host": path.dst_host,
                    "devices": path.devices,
                    "switches": path.switches,
                    "available": not path_errors and path.enabled,
                    "errors": [asdict(item) for item in path_errors],
                }
            )
        endpoint_pairs = {
            (item.src_host, item.dst_host)
            for item in self._paths.values()
            if item.enabled
        }
        endpoints_without_path = [
            f"{src}->{dst}"
            for src, dst in sorted(endpoint_pairs)
            if not any(
                item.path_id in available
                and item.src_host == src
                and item.dst_host == dst
                for item in self._paths.values()
            )
        ]
        if self.errors or endpoints_without_path:
            runtime_status = "invalid"
        elif unavailable:
            runtime_status = "degraded"
        else:
            runtime_status = "ok"
        return {
            "status": runtime_status,
            "config_path": self.config_path,
            "schema_version": self.schema_version,
            "topology_name": self.topology_name,
            "configured_paths": len(self._paths),
            "available_path_ids": available,
            "unavailable_path_ids": unavailable,
            "endpoints_without_available_path": endpoints_without_path,
            "errors": [asdict(item) for item in errors],
            "paths": path_reports,
        }

    def select_available_path(
        self,
        src_host: str,
        dst_host: str,
        topology: dict[str, Any],
    ) -> TopologyPath | None:
        report = self.validate_runtime(topology)
        available = set(report["available_path_ids"])
        candidates = [
            path
            for path in self._paths.values()
            if path.path_id in available
            and path.src_host == src_host
            and path.dst_host == dst_host
        ]
        candidates.sort(
            key=lambda item: (0 if item.role == "PRIMARY" else 1, item.path_id)
        )
        return candidates[0] if candidates else None

    def report(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.valid else "invalid",
            "config_path": self.config_path,
            "schema_version": self.schema_version,
            "topology_name": self.topology_name,
            "path_ids": sorted(self._paths),
            "physical_link_count": len(self.physical_links),
            "errors": [asdict(item) for item in self.errors],
        }

    def _load(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            self.errors.append(
                PathValidationError(
                    PATH_CONFIG_INVALID, None, f"missing {self.config_path}"
                )
            )
            logger.error("topology path configuration missing: %s", self.config_path)
            return
        except (json.JSONDecodeError, OSError) as exc:
            self.errors.append(PathValidationError(PATH_CONFIG_INVALID, None, str(exc)))
            logger.exception("failed to load topology path configuration")
            return

        self.schema_version = str(raw.get("schema_version") or "")
        self.topology_name = str(raw.get("topology_name") or "")
        self.device_switch_map = {
            str(key): str(value)
            for key, value in (raw.get("device_switch_map") or {}).items()
        }
        self.host_attachment_map = dict(raw.get("host_attachment_map") or {})
        self.physical_links = [
            dict(item)
            for item in (raw.get("physical_links") or [])
            if isinstance(item, dict)
        ]
        path_items = raw.get("paths") or []
        if isinstance(path_items, dict):
            path_items = [
                {"path_id": key, **value} for key, value in path_items.items()
            ]
        for item in path_items:
            try:
                path = TopologyPath(
                    path_id=str(item["path_id"]),
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    src_host=str(item["src_host"]),
                    dst_host=str(item["dst_host"]),
                    role=str(item["role"]).upper(),
                    devices=[str(value) for value in item["devices"]],
                    switches=[str(value) for value in item["switches"]],
                    expected_links=[
                        ExpectedPathLink(**value) for value in item["expected_links"]
                    ],
                    qos_egress_candidates=[
                        QosEgressCandidate(**value)
                        for value in item["qos_egress_candidates"]
                    ],
                    path_capacity_mbps=int(item["path_capacity_mbps"]),
                    bottleneck_link_id=str(item["bottleneck_link_id"]),
                    enabled=bool(item.get("enabled", True)),
                    service_id=str(item.get("service_id") or ""),
                )
            except (KeyError, TypeError, ValueError) as exc:
                self.errors.append(
                    PathValidationError(
                        PATH_CONFIG_INVALID,
                        str(item.get("path_id") or "") or None,
                        str(exc),
                    )
                )
                continue
            if path.path_id in self._paths:
                self.errors.append(
                    PathValidationError(
                        PATH_CONFIG_INVALID, path.path_id, "duplicate path_id"
                    )
                )
                continue
            self._paths[path.path_id] = path
            self.errors.extend(self._validate_template(path))
        self.errors.extend(self._validate_primary_backup_pairs())
        if self.valid:
            logger.info(
                "loaded %d validated topology paths from %s",
                len(self._paths),
                self.config_path,
            )
        else:
            logger.error(
                "topology path configuration invalid: %s",
                [asdict(item) for item in self.errors],
            )

    def _validate_template(self, path: TopologyPath) -> list[PathValidationError]:
        errors: list[PathValidationError] = []
        if path.role not in {"PRIMARY", "BACKUP"} or len(path.devices) < 2:
            errors.append(
                PathValidationError(
                    PATH_CONFIG_INVALID,
                    path.path_id,
                    "invalid role or empty device path",
                )
            )
        if (
            len(path.devices) != len(path.switches)
            or len(path.expected_links) != len(path.devices) - 1
        ):
            errors.append(
                PathValidationError(
                    PATH_CONFIG_INVALID,
                    path.path_id,
                    "device/switch/link sequence lengths do not align",
                )
            )
        for device, switch in zip(path.devices, path.switches):
            if self.device_switch_map.get(device) != switch:
                errors.append(
                    PathValidationError(
                        PATH_CONFIG_INVALID,
                        path.path_id,
                        f"device/switch mismatch {device}/{switch}",
                    )
                )
        source = self.host_attachment_map.get(path.src_host) or {}
        destination = self.host_attachment_map.get(path.dst_host) or {}
        if (
            source.get("device_id") != path.devices[0]
            or destination.get("device_id") != path.devices[-1]
        ):
            errors.append(
                PathValidationError(
                    PATH_ENDPOINT_MISMATCH,
                    path.path_id,
                    "host attachment does not match path endpoints",
                )
            )
        for index, link in enumerate(path.expected_links):
            if (
                link.from_device != path.devices[index]
                or link.to_device != path.devices[index + 1]
            ):
                errors.append(
                    PathValidationError(
                        PATH_LINK_MISSING,
                        path.path_id,
                        f"non-contiguous expected link {link.link_id}",
                    )
                )
            physical = next(
                (
                    item
                    for item in self.physical_links
                    if tuple(
                        sorted((str(item.get("device_a")), str(item.get("device_b"))))
                    )
                    == link.device_pair
                ),
                None,
            )
            if physical is None:
                errors.append(
                    PathValidationError(
                        PATH_LINK_MISSING,
                        path.path_id,
                        f"{link.link_id} is absent from physical_links",
                    )
                )
            else:
                forward = physical.get("device_a") == link.from_device
                raw_from_port = (
                    physical.get("port_a") if forward else physical.get("port_b")
                )
                raw_to_port = (
                    physical.get("port_b") if forward else physical.get("port_a")
                )
                if raw_from_port is None or raw_to_port is None:
                    errors.append(
                        PathValidationError(
                            PATH_LINK_MISSING,
                            path.path_id,
                            f"{link.link_id} has no port mapping",
                        )
                    )
                    continue
                expected_from_port = int(raw_from_port)
                expected_to_port = int(raw_to_port)
                if (
                    str(physical.get("link_id")) != link.link_id
                    or expected_from_port != link.from_port
                    or expected_to_port != link.to_port
                ):
                    errors.append(
                        PathValidationError(
                            PATH_LINK_MISSING,
                            path.path_id,
                            f"{link.link_id} device/port mapping differs from physical_links",
                        )
                    )
        path_link_ids = {item.link_id for item in path.expected_links}
        for candidate in path.qos_egress_candidates:
            expected = next(
                (
                    item
                    for item in path.expected_links
                    if item.link_id == candidate.link_id
                    and item.from_device == candidate.device_id
                    and item.to_device == candidate.peer_device_id
                ),
                None,
            )
            mapping_valid = (
                self.device_switch_map.get(candidate.device_id) == candidate.switch
                and self.device_switch_map.get(candidate.peer_device_id)
                == candidate.peer_switch
            )
            interface_valid = (
                candidate.interface == f"{candidate.switch}-eth{candidate.egress_port}"
            )
            if (
                candidate.device_id not in path.devices
                or candidate.peer_device_id not in path.devices
                or candidate.link_id not in path_link_ids
                or expected is None
                or candidate.egress_port != expected.from_port
                or not mapping_valid
                or not interface_valid
            ):
                errors.append(
                    PathValidationError(
                        QOS_EGRESS_NOT_ON_PATH,
                        path.path_id,
                        f"QoS egress {candidate.interface} is not on path",
                    )
                )
        return errors

    def _validate_primary_backup_pairs(self) -> list[PathValidationError]:
        errors: list[PathValidationError] = []
        endpoints = {(item.src_host, item.dst_host) for item in self._paths.values()}
        for endpoint in endpoints:
            primaries = [
                item
                for item in self._paths.values()
                if (item.src_host, item.dst_host) == endpoint and item.role == "PRIMARY"
            ]
            backups = [
                item
                for item in self._paths.values()
                if (item.src_host, item.dst_host) == endpoint and item.role == "BACKUP"
            ]
            if not primaries or not backups:
                errors.append(
                    PathValidationError(
                        PATH_CONFIG_INVALID,
                        None,
                        f"endpoint {endpoint[0]}->{endpoint[1]} requires both PRIMARY and BACKUP paths",
                    )
                )
            for primary in primaries:
                for backup in backups:
                    if primary.devices == backup.devices:
                        errors.append(
                            PathValidationError(
                                BACKUP_PATH_IDENTICAL,
                                backup.path_id,
                                f"backup path equals {primary.path_id}",
                            )
                        )
        return errors

    def __repr__(self) -> str:
        return f"TopologyPathRegistry(valid={self.valid}, paths={list(self._paths)})"


topology_paths = TopologyPathRegistry()
