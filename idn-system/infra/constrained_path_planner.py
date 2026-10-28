"""Constraint-aware path computation over the live resource graph."""
from __future__ import annotations

import heapq
from typing import Any

from infra.resource_ledger import ResourceLedger
from models.faults import ConstrainedPath, PathConstraints


def canonical_link_id(left: str, right: str) -> str:
    return "link:" + "--".join(sorted((str(left), str(right))))


class ConstrainedPathPlanner:
    def __init__(self, ledger: ResourceLedger | None = None):
        self.ledger = ledger or ResourceLedger()

    def _available(self) -> dict[str, float]:
        return {item.resource_id: item.available_guaranteed_mbps for item in self.ledger.entries()}

    def compute(
        self, topology: dict[str, Any], source: str, destination: str,
        constraints: PathConstraints | None = None,
    ) -> ConstrainedPath:
        constraints = constraints or PathConstraints()
        waypoints = [source, *constraints.required_devices, destination]
        nodes: list[str] = []
        links: list[dict[str, Any]] = []
        available = self._available()
        for left, right in zip(waypoints, waypoints[1:]):
            segment_nodes, segment_links = self._shortest(
                topology, left, right, constraints, available,
            )
            nodes.extend(segment_nodes if not nodes else segment_nodes[1:])
            links.extend(segment_links)
        total_latency = sum(float(item.get("delay_ms") or 0) for item in links)
        if constraints.max_latency_ms is not None and total_latency > constraints.max_latency_ms:
            raise ValueError(
                f"NO_FEASIBLE_PATH: latency {total_latency:g}ms exceeds {constraints.max_latency_ms:g}ms"
            )
        canonical_ids = [
            canonical_link_id(item["endpoints"][0]["node"], item["endpoints"][1]["node"])
            for item in links
        ]
        bottleneck = min((available.get(item) for item in canonical_ids if item in available), default=None)
        total_cost = sum(self._edge_score(item, available.get(link_id)) for item, link_id in zip(links, canonical_ids))
        return ConstrainedPath(
            source=source, destination=destination, nodes=nodes,
            link_ids=[str(item.get("id") or canonical) for item, canonical in zip(links, canonical_ids)],
            total_cost=total_cost, estimated_latency_ms=total_latency,
            bottleneck_available_mbps=bottleneck,
            score_components={"topology_cost": sum(float(item.get("cost") or 1) for item in links),
                              "latency_ms": total_latency,
                              "reservation_penalty": max(0.0, total_cost - sum(float(item.get("cost") or 1) for item in links) - total_latency * 0.05)},
        )

    @staticmethod
    def _edge_score(link: dict[str, Any], available: float | None) -> float:
        capacity = float(link.get("bandwidth_mbps") or 0)
        reservation_penalty = 0.0
        if available is not None and capacity > 0:
            reservation_penalty = max(0.0, 1.0 - available / capacity) * 10.0
        return float(link.get("cost") or 1) + float(link.get("delay_ms") or 0) * 0.05 + reservation_penalty

    def _shortest(
        self, topology: dict[str, Any], source: str, destination: str,
        constraints: PathConstraints, available: dict[str, float],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        graph: dict[str, list[tuple[float, str, dict[str, Any]]]] = {}
        avoid_devices = {str(item) for item in constraints.avoid_devices}
        avoid_links = {str(item) for item in constraints.avoid_links}
        for link in topology.get("links") or []:
            endpoints = link.get("endpoints") or []
            if len(endpoints) != 2 or str(link.get("state") or "ACTIVE").upper() != "ACTIVE":
                continue
            a, b = str(endpoints[0].get("node")), str(endpoints[1].get("node"))
            canonical = canonical_link_id(a, b)
            if a in avoid_devices or b in avoid_devices or str(link.get("id")) in avoid_links or canonical in avoid_links:
                continue
            capacity = float(link.get("bandwidth_mbps") or 0)
            effective_available = available.get(canonical, capacity)
            if constraints.min_bandwidth_mbps is not None and effective_available < constraints.min_bandwidth_mbps:
                continue
            score = self._edge_score(link, effective_available)
            graph.setdefault(a, []).append((score, b, link))
            graph.setdefault(b, []).append((score, a, link))

        queue = [(0.0, source)]
        distance = {source: 0.0}
        previous: dict[str, tuple[str, dict[str, Any]]] = {}
        while queue:
            score, node = heapq.heappop(queue)
            if node == destination:
                break
            if score != distance.get(node):
                continue
            for edge_score, neighbor, link in graph.get(node, []):
                candidate = score + edge_score
                if candidate < distance.get(neighbor, float("inf")):
                    distance[neighbor] = candidate
                    previous[neighbor] = (node, link)
                    heapq.heappush(queue, (candidate, neighbor))
        if destination not in distance:
            raise ValueError(f"NO_FEASIBLE_PATH: {source} -> {destination}")
        path_nodes, path_links, current = [destination], [], destination
        while current != source:
            parent, link = previous[current]
            path_nodes.append(parent)
            path_links.append(link)
            current = parent
        return list(reversed(path_nodes)), list(reversed(path_links))
