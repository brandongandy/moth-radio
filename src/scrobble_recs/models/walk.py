"""Personalized-PageRank scorer over the artist similarity graph.

The walk gives multi-hop reach and principled seed weighting. Raw PPR mass is
popularity-biased by construction, so candidates are ranked on *lift* over a
non-personalized baseline walk rather than on raw mass.

Everything here is deterministic: nodes are ordered by a stable key (artist id),
computations run in float64, and tie-breaking is explicit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import log

import numpy as np
import numpy.typing as npt
from scipy.sparse import csr_matrix

from scrobble_recs.config import WalkConfig

_EPS = 1e-12
# The spec floors convergence at L1 < 1e-9; we iterate past it to 1e-12 so the
# result is converged tightly enough to (a) match a dense reference solve to
# 1e-10 and (b) be byte-reproducible run-to-run. Still bounded by _MAX_ITERATIONS.
_L1_TOLERANCE = 1e-12
_MAX_ITERATIONS = 200


@dataclass(frozen=True)
class WalkNode:
    artist_id: int
    ppr_personalized: float
    ppr_uniform: float
    lift: float
    hop_distance: int
    distinct_seed_count: int


@dataclass(frozen=True)
class WalkDiagnostics:
    node_count: int
    edge_count: int
    iterations: int
    final_delta: float
    node_cap_bound: bool
    iteration_cap_bound: bool


@dataclass(frozen=True)
class WalkPath:
    node_ids: tuple[int, ...]
    weight: float


@dataclass(frozen=True)
class WalkResult:
    nodes: dict[int, WalkNode]
    diagnostics: WalkDiagnostics
    # Row-normalized forward adjacency retained for deterministic path search.
    _forward: dict[int, dict[int, float]]


def build_walk_result(
    similarity_by_target: dict[int, list[tuple[int, float]]],
    *,
    seed_ids: set[int],
    seed_weights: dict[int, float],
    config: WalkConfig,
) -> WalkResult:
    raw_forward = _forward_adjacency(similarity_by_target, symmetrize=config.symmetrize)
    seed_reach = _seed_reachability(raw_forward, seed_ids, max_hops=config.max_hops)

    node_set, node_cap_bound = _select_nodes(
        seed_reach.reachable,
        seed_ids=seed_ids,
        similarity_by_target=similarity_by_target,
        max_nodes=config.max_nodes,
    )
    node_ids = sorted(node_set)
    index_by_id = {artist_id: index for index, artist_id in enumerate(node_ids)}

    forward = _restrict_and_row_normalize(raw_forward, node_set)
    transition, dangling = _build_transition(forward, node_ids, index_by_id)

    personalization = _personalization_vector(node_ids, index_by_id, seed_weights, seed_ids)
    uniform = np.full(len(node_ids), 1.0 / len(node_ids), dtype=np.float64)

    personalized_mass, iterations, delta = _power_iteration(
        transition, dangling, personalization, restart=config.restart
    )
    uniform_mass, uniform_iterations, uniform_delta = _power_iteration(
        transition, dangling, uniform, restart=config.restart
    )
    iteration_cap_bound = (iterations >= _MAX_ITERATIONS) or (uniform_iterations >= _MAX_ITERATIONS)

    nodes: dict[int, WalkNode] = {}
    for artist_id in node_ids:
        i = index_by_id[artist_id]
        personalized = float(personalized_mass[i])
        baseline = float(uniform_mass[i])
        nodes[artist_id] = WalkNode(
            artist_id=artist_id,
            ppr_personalized=personalized,
            ppr_uniform=baseline,
            lift=_lift(personalized, baseline, baseline_exponent=config.baseline_exponent),
            hop_distance=seed_reach.hop_distance.get(artist_id, -1),
            distinct_seed_count=seed_reach.distinct_seed_count.get(artist_id, 0),
        )

    diagnostics = WalkDiagnostics(
        node_count=len(node_ids),
        edge_count=sum(len(targets) for targets in forward.values()),
        iterations=max(iterations, uniform_iterations),
        final_delta=max(delta, uniform_delta),
        node_cap_bound=node_cap_bound,
        iteration_cap_bound=iteration_cap_bound,
    )
    return WalkResult(nodes=nodes, diagnostics=diagnostics, _forward=forward)


def _lift(personalized: float, baseline: float, *, baseline_exponent: float) -> float:
    if personalized <= 0.0:
        return float("-inf")
    return log(personalized) - log(baseline**baseline_exponent + _EPS)


def _forward_adjacency(
    similarity_by_target: dict[int, list[tuple[int, float]]],
    *,
    symmetrize: bool,
) -> dict[int, dict[int, float]]:
    """Invert target-keyed edges into source -> {target: weight}.

    Self-loops are dropped; when several rows describe the same directed edge the
    strongest weight wins (deterministic, order-independent).
    """
    forward: dict[int, dict[int, float]] = {}
    for target, sources in similarity_by_target.items():
        for source, weight in sources:
            if source == target:
                continue
            neighbors = forward.setdefault(source, {})
            neighbors[target] = max(neighbors.get(target, 0.0), float(weight))
    if not symmetrize:
        return forward
    symmetric: dict[int, dict[int, float]] = {}
    for source, targets in forward.items():
        for target, weight in targets.items():
            reverse = forward.get(target, {}).get(source, 0.0)
            strongest = max(weight, reverse)
            symmetric.setdefault(source, {})[target] = strongest
            symmetric.setdefault(target, {})[source] = strongest
    return symmetric


@dataclass(frozen=True)
class _SeedReachability:
    reachable: set[int]
    hop_distance: dict[int, int]
    distinct_seed_count: dict[int, int]


def _seed_reachability(
    forward: dict[int, dict[int, float]],
    seed_ids: set[int],
    *,
    max_hops: int,
) -> _SeedReachability:
    reachable: set[int] = set()
    hop_distance: dict[int, int] = {}
    distinct_seed_count: dict[int, int] = {}
    for seed in sorted(seed_ids):
        distances = _bounded_bfs(forward, seed, max_hops=max_hops)
        for node, distance in distances.items():
            reachable.add(node)
            if node not in hop_distance or distance < hop_distance[node]:
                hop_distance[node] = distance
            distinct_seed_count[node] = distinct_seed_count.get(node, 0) + 1
    return _SeedReachability(reachable, hop_distance, distinct_seed_count)


def _bounded_bfs(
    forward: dict[int, dict[int, float]],
    origin: int,
    *,
    max_hops: int,
) -> dict[int, int]:
    distances = {origin: 0}
    queue: deque[int] = deque([origin])
    while queue:
        current = queue.popleft()
        depth = distances[current]
        if depth >= max_hops:
            continue
        for neighbor in sorted(forward.get(current, {})):
            if neighbor not in distances:
                distances[neighbor] = depth + 1
                queue.append(neighbor)
    return distances


def _select_nodes(
    reachable: set[int],
    *,
    seed_ids: set[int],
    similarity_by_target: dict[int, list[tuple[int, float]]],
    max_nodes: int,
) -> tuple[set[int], bool]:
    nodes = set(reachable) | set(seed_ids)
    if len(nodes) <= max_nodes:
        return nodes, False
    # Cap binds: keep seeds plus the highest aggregate-seed-strength nodes.
    aggregate = _aggregate_seed_strength(similarity_by_target, seed_ids)
    non_seed = sorted(
        (node for node in nodes if node not in seed_ids),
        key=lambda node: (-aggregate.get(node, 0.0), node),
    )
    budget = max(0, max_nodes - len(seed_ids))
    kept = set(seed_ids) | set(non_seed[:budget])
    return kept, True


def _aggregate_seed_strength(
    similarity_by_target: dict[int, list[tuple[int, float]]],
    seed_ids: set[int],
) -> dict[int, float]:
    aggregate: dict[int, float] = {}
    for target, sources in similarity_by_target.items():
        for source, weight in sources:
            if source in seed_ids:
                aggregate[target] = aggregate.get(target, 0.0) + float(weight)
    return aggregate


def _restrict_and_row_normalize(
    forward: dict[int, dict[int, float]],
    node_set: set[int],
) -> dict[int, dict[int, float]]:
    restricted: dict[int, dict[int, float]] = {}
    for source in sorted(node_set):
        targets = {
            target: weight
            for target, weight in forward.get(source, {}).items()
            if target in node_set and weight > 0.0
        }
        out_sum = sum(targets.values())
        if out_sum <= 0.0:
            restricted[source] = {}
            continue
        restricted[source] = {target: weight / out_sum for target, weight in targets.items()}
    return restricted


def _build_transition(
    forward: dict[int, dict[int, float]],
    node_ids: list[int],
    index_by_id: dict[int, int],
) -> tuple[csr_matrix, npt.NDArray[np.float64]]:
    """Column-stochastic transition M where M[j, i] = P(i -> j)."""
    n = len(node_ids)
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    dangling = np.ones(n, dtype=np.float64)
    for source in node_ids:
        i = index_by_id[source]
        targets = forward.get(source, {})
        if targets:
            dangling[i] = 0.0
        for target, weight in targets.items():
            rows.append(index_by_id[target])
            cols.append(i)
            data.append(weight)
    transition = csr_matrix((data, (rows, cols)), shape=(n, n), dtype=np.float64)
    return transition, dangling


def _personalization_vector(
    node_ids: list[int],
    index_by_id: dict[int, int],
    seed_weights: dict[int, float],
    seed_ids: set[int],
) -> npt.NDArray[np.float64]:
    vector = np.zeros(len(node_ids), dtype=np.float64)
    for seed in sorted(seed_ids):
        if seed in index_by_id:
            vector[index_by_id[seed]] = max(0.0, float(seed_weights.get(seed, 0.0)))
    total = float(vector.sum())
    if total <= 0.0:
        # Degenerate (e.g. all seed weights zeroed): fall back to uniform over seeds.
        for seed in sorted(seed_ids):
            if seed in index_by_id:
                vector[index_by_id[seed]] = 1.0
        total = float(vector.sum())
    if total <= 0.0:
        return np.full(len(node_ids), 1.0 / len(node_ids), dtype=np.float64)
    return vector / total


def _power_iteration(
    transition: csr_matrix,
    dangling: npt.NDArray[np.float64],
    personalization: npt.NDArray[np.float64],
    *,
    restart: float,
) -> tuple[npt.NDArray[np.float64], int, float]:
    p = personalization.copy()
    iterations = 0
    delta = float("inf")
    for step in range(1, _MAX_ITERATIONS + 1):
        iterations = step
        dangling_mass = float(dangling @ p)
        p_next = (1.0 - restart) * transition.dot(p)
        p_next += (restart + (1.0 - restart) * dangling_mass) * personalization
        delta = float(np.abs(p_next - p).sum())
        p = p_next
        if delta < _L1_TOLERANCE:
            break
    return p, iterations, delta


def top_paths(
    result: WalkResult,
    *,
    target: int,
    seed_ids: set[int],
    max_paths: int = 3,
    beam_width: int = 16,
) -> list[WalkPath]:
    """Top weighted seed->target paths via deterministic reverse beam search.

    Path weight is the product of row-normalized edge weights. Ties break on the
    node-id sequence so the same graph always yields the same paths.
    """
    reverse: dict[int, list[tuple[int, float]]] = {}
    for source, targets in result._forward.items():
        for neighbor, weight in targets.items():
            reverse.setdefault(neighbor, []).append((source, weight))

    # Each beam entry is (path ending at `target`, accumulated weight), grown
    # backward toward a seed.
    beams: list[tuple[tuple[int, ...], float]] = [((target,), 1.0)]
    completed: list[WalkPath] = []
    max_depth = max((node.hop_distance for node in result.nodes.values()), default=0) + 1
    for _ in range(max_depth):
        if not beams:
            break
        expanded: list[tuple[tuple[int, ...], float]] = []
        for path, weight in beams:
            head = path[0]
            if head in seed_ids and len(path) > 1:
                completed.append(WalkPath(node_ids=path, weight=weight))
                continue
            for predecessor, edge_weight in sorted(reverse.get(head, [])):
                if predecessor in path:
                    continue
                expanded.append(((predecessor, *path), weight * edge_weight))
        expanded.sort(key=lambda item: (-item[1], item[0]))
        beams = expanded[:beam_width]
    completed.extend(
        WalkPath(node_ids=path, weight=weight)
        for path, weight in beams
        if path[0] in seed_ids and len(path) > 1
    )
    completed.sort(key=lambda p: (-p.weight, p.node_ids))
    deduped: list[WalkPath] = []
    seen: set[tuple[int, ...]] = set()
    for candidate_path in completed:
        if candidate_path.node_ids in seen:
            continue
        seen.add(candidate_path.node_ids)
        deduped.append(candidate_path)
    return deduped[:max_paths]
