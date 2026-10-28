"""Deterministic validation and projection of business preference relations.

The service intentionally has no southbound dependencies.  Preferences only
rank otherwise-feasible choices; hard SLA, capacity admission and policy
ownership remain authoritative.  Keeping this boundary pure also makes a
cycle rejection provably free of ONOS/P4/TC writes.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable, Sequence

from models.intent_domain import PreferenceDimension, PreferenceRelation


class PreferenceCycleError(ValueError):
    code = "PREFERENCE_CYCLE"

    def __init__(self, dimension: PreferenceDimension, cycle: list[str]):
        self.dimension = dimension
        self.cycle = cycle
        super().__init__(
            f"{self.code}: {dimension.value} cycle " + " -> ".join(cycle)
        )


def validate_acyclic(relations: Iterable[PreferenceRelation]) -> None:
    """Reject a directed cycle separately for each preference dimension."""
    by_dimension: dict[PreferenceDimension, list[PreferenceRelation]] = defaultdict(list)
    for relation in relations:
        by_dimension[relation.dimension].append(relation)
    for dimension, items in by_dimension.items():
        graph: dict[str, list[str]] = defaultdict(list)
        nodes: set[str] = set()
        for item in items:
            higher, lower = item.higher_scope.casefold(), item.lower_scope.casefold()
            graph[higher].append(lower)
            nodes.update((higher, lower))
        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []

        def visit(node: str) -> None:
            if node in visiting:
                start = stack.index(node)
                raise PreferenceCycleError(dimension, [*stack[start:], node])
            if node in visited:
                return
            visiting.add(node)
            stack.append(node)
            for next_node in graph[node]:
                visit(next_node)
            stack.pop()
            visiting.remove(node)
            visited.add(node)

        for node in sorted(nodes):
            visit(node)


def scheduler_projection(relations: Iterable[PreferenceRelation]) -> dict[str, object]:
    """Create an auditable stable rank map without changing queue parameters."""
    service_relations = [
        item for item in relations
        if item.dimension == PreferenceDimension.SERVICE_PRIORITY
    ]
    validate_acyclic(service_relations)
    nodes = {
        scope.casefold()
        for item in service_relations
        for scope in (item.higher_scope, item.lower_scope)
    }
    outgoing: dict[str, set[str]] = defaultdict(set)
    indegree = {node: 0 for node in nodes}
    for item in service_relations:
        higher, lower = item.higher_scope.casefold(), item.lower_scope.casefold()
        if lower not in outgoing[higher]:
            outgoing[higher].add(lower)
            indegree[lower] += 1
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    ordered: list[str] = []
    while ready:
        node = ready.popleft()
        ordered.append(node)
        for child in sorted(outgoing[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return {
        "execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE",
        "dimension": PreferenceDimension.SERVICE_PRIORITY.value,
        "ranked_scopes": ordered,
        "ranks": {scope: index + 1 for index, scope in enumerate(ordered)},
        "southbound_mutation_required": False,
    }


def path_preference_projection(
    relations: Iterable[PreferenceRelation],
    *,
    src_host: str,
    dst_host: str,
    service_id: str,
    runtime_topology: dict[str, object],
) -> dict[str, object]:
    """Resolve path roles through registered PathConfig plus live topology.

    The result is a scheduler selection/readback only: it never derives
    topology from a business name and never alters forwarding state.
    """
    path_relations = [
        item for item in relations
        if item.dimension == PreferenceDimension.PATH_PREFERENCE
    ]
    validate_acyclic(path_relations)
    if not path_relations:
        return {}

    from api.topology_paths import TopologyPathRegistry

    report = TopologyPathRegistry().validate_runtime(runtime_topology)
    requested_roles = {
        scope.casefold()
        for relation in path_relations
        for scope in (relation.higher_scope, relation.lower_scope)
    }
    candidates = [
        item for item in report["paths"]
        if item.get("src_host") == src_host
        and item.get("dst_host") == dst_host
        and item.get("service_id") == service_id
        and str(item.get("role", "")).casefold() in requested_roles
    ]
    if not candidates:
        raise ValueError(
            "PATH_PREFERENCE_NO_CONFIGURED_CANDIDATES: "
            f"{src_host}->{dst_host} roles={sorted(requested_roles)}"
        )
    ranks: dict[str, int] = {}
    for relation in path_relations:
        ranks[relation.higher_scope.casefold()] = ranks.get(
            relation.higher_scope.casefold(), 0
        ) + 1
        ranks.setdefault(relation.lower_scope.casefold(), 0)
    normalized = [
        {
            "path_id": item["path_id"],
            "role": item["role"],
            "available": bool(item["available"]),
            "switches": list(item.get("switches") or []),
            "errors": list(item.get("errors") or []),
            "preference_rank": ranks.get(str(item["role"]).casefold(), 0),
        }
        for item in candidates
    ]
    selected = next(
        (
            item for item in sorted(
                normalized,
                key=lambda candidate: (
                    not candidate["available"],
                    -int(candidate["preference_rank"]),
                    str(candidate["path_id"]),
                ),
            )
            if item["available"]
        ),
        None,
    )
    if selected is None:
        raise ValueError(
            f"PATH_PREFERENCE_NO_AVAILABLE_CANDIDATE: {src_host}->{dst_host}"
        )
    fallback = [
        item for item in normalized
        if item["path_id"] != selected["path_id"] and item["available"]
    ]
    return {
        "execution_capability": "CONTROL_PLANE_SCHEDULING_PREFERENCE",
        "dimension": PreferenceDimension.PATH_PREFERENCE.value,
        "source_host": src_host,
        "destination_host": dst_host,
        "service_id": service_id,
        "candidates": normalized,
        "selected_path_id": selected["path_id"],
        "selected_role": selected["role"],
        "fallback_path_ids": [item["path_id"] for item in fallback],
        "fallback_semantics": "select next available configured candidate only when selected candidate is unavailable",
        "runtime_topology_status": report["status"],
        "southbound_mutation_required": False,
    }


def rank_path_candidates(
    candidates: Sequence[object], relations: Iterable[PreferenceRelation]
) -> dict[str, int]:
    """Return additive, generic path-preference bonuses keyed by candidate id.

    Candidate producers may expose a ``preference_scopes`` attribute (or a
    dictionary key) containing configured path roles.  This avoids encoding
    topology/path names in the preference engine.
    """
    path_relations = [
        item for item in relations
        if item.dimension == PreferenceDimension.PATH_PREFERENCE
    ]
    validate_acyclic(path_relations)
    bonuses: dict[str, int] = {}
    for candidate in candidates:
        candidate_id = str(getattr(candidate, "candidate_id", ""))
        scopes = getattr(candidate, "preference_scopes", None)
        if scopes is None and isinstance(candidate, dict):
            candidate_id = str(candidate.get("candidate_id") or candidate_id)
            scopes = candidate.get("preference_scopes")
        labels = {str(item).casefold() for item in (scopes or [])}
        bonus = sum(
            1 for relation in path_relations
            if relation.higher_scope.casefold() in labels
        )
        bonuses[candidate_id] = bonus
    return bonuses
