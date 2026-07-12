from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from scrobble_recs.config import SerendipityConfig


@dataclass(frozen=True)
class SerendipityEvidence:
    neighbors: tuple[int, ...]
    cohesion: float
    component_ids: tuple[int, ...]
    is_bridge: bool
    raw_bonus: float
    calibrated_bonus: float


def score_serendipity(
    *,
    candidate_edges: Sequence[tuple[int, float]],
    seed_graph: Mapping[int, Mapping[int, float]],
    config: SerendipityConfig,
) -> SerendipityEvidence:
    strongest_edge_by_seed: dict[int, float] = {}
    for seed_id, edge_weight in candidate_edges:
        strongest_edge_by_seed[seed_id] = max(
            edge_weight,
            strongest_edge_by_seed.get(seed_id, 0.0),
        )
    neighbors = tuple(
        seed_id
        for seed_id, edge_weight in sorted(strongest_edge_by_seed.items())
        if edge_weight >= config.minimum_neighbor_edge
    )
    if not config.enabled or len(neighbors) < config.minimum_neighbors:
        return SerendipityEvidence(neighbors, 0.0, (), False, 0.0, 0.0)

    cohesion = _mean_pair_weight(neighbors, seed_graph)
    component_ids = _connected_components(seed_graph, neighbors)
    is_bridge = len(set(component_ids)) > 1
    raw_bonus = max(
        0.0,
        min(
            config.max_bonus,
            config.max_bonus * (1.0 - cohesion) + (config.bridge_bonus if is_bridge else 0.0),
        ),
    )
    return SerendipityEvidence(
        neighbors,
        cohesion,
        component_ids,
        is_bridge,
        raw_bonus,
        raw_bonus,
    )


def _mean_pair_weight(
    neighbors: Sequence[int], seed_graph: Mapping[int, Mapping[int, float]]
) -> float:
    weights = [_pair_weight(left, right, seed_graph) for left, right in combinations(neighbors, 2)]
    return sum(weights) / len(weights) if weights else 0.0


def _connected_components(
    seed_graph: Mapping[int, Mapping[int, float]], neighbors: Sequence[int]
) -> tuple[int, ...]:
    adjacency = _undirected_adjacency(seed_graph)
    component_by_seed: dict[int, int] = {}
    for seed_id in neighbors:
        if seed_id in component_by_seed:
            continue
        component = _connected_component(adjacency, seed_id)
        component_id = min(component)
        component_by_seed.update({member: component_id for member in component})
    return tuple(component_by_seed[seed_id] for seed_id in neighbors)


def _connected_component(adjacency: Mapping[int, set[int]], origin: int) -> set[int]:
    visited: set[int] = set()
    pending = [origin]
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        pending.extend(adjacency.get(current, set()) - visited)
    return visited


def _undirected_adjacency(
    seed_graph: Mapping[int, Mapping[int, float]],
) -> dict[int, set[int]]:
    adjacency: dict[int, set[int]] = {}
    for source, targets in seed_graph.items():
        adjacency.setdefault(source, set())
        for target, weight in targets.items():
            if weight <= 0.0:
                continue
            adjacency.setdefault(target, set()).add(source)
            adjacency[source].add(target)
    return adjacency


def _pair_weight(left: int, right: int, seed_graph: Mapping[int, Mapping[int, float]]) -> float:
    return max(
        seed_graph.get(left, {}).get(right, 0.0),
        seed_graph.get(right, {}).get(left, 0.0),
    )
