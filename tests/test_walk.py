from __future__ import annotations

import numpy as np

from scrobble_recs.config import WalkConfig
from scrobble_recs.models.walk import build_walk_result, top_paths

# source -> {target: weight}
_EDGES: dict[int, dict[int, float]] = {
    1: {3: 0.9, 4: 0.5},
    2: {4: 0.8, 5: 0.6},
    3: {6: 0.7},
    4: {6: 0.4},
    5: {2: 0.3},
    6: {},
}


def _similarity_by_target(
    edges: dict[int, dict[int, float]],
) -> dict[int, list[tuple[int, float]]]:
    by_target: dict[int, list[tuple[int, float]]] = {}
    for source, targets in edges.items():
        for target, weight in targets.items():
            by_target.setdefault(target, []).append((source, weight))
    return by_target


def _dense_ppr(
    edges: dict[int, dict[int, float]],
    seed_weights: dict[int, float],
    *,
    restart: float,
) -> dict[int, float]:
    """Reference personalized PageRank via a dense linear solve.

    Dangling columns redistribute to the personalization vector, so the
    transition matrix M' is column-stochastic and p = r (I - (1-r) M')^-1 v.
    """
    nodes = sorted(set(edges) | {t for targets in edges.values() for t in targets})
    index = {node: i for i, node in enumerate(nodes)}
    n = len(nodes)

    total_seed = sum(seed_weights.values())
    v = np.zeros(n)
    for seed, weight in seed_weights.items():
        v[index[seed]] = weight / total_seed

    m = np.zeros((n, n))
    for source in nodes:
        targets = edges.get(source, {})
        out_sum = sum(targets.values())
        if out_sum <= 0.0:
            m[:, index[source]] = v  # dangling -> personalization
            continue
        for target, weight in targets.items():
            m[index[target], index[source]] = weight / out_sum

    p = restart * np.linalg.solve(np.eye(n) - (1.0 - restart) * m, v)
    return {node: float(p[index[node]]) for node in nodes}


def test_sparse_ppr_matches_dense_reference() -> None:
    config = WalkConfig(max_hops=5, restart=0.15, symmetrize=False)
    result = build_walk_result(
        _similarity_by_target(_EDGES),
        seed_ids={1, 2},
        seed_weights={1: 1.0, 2: 1.0},
        config=config,
    )
    reference = _dense_ppr(_EDGES, {1: 1.0, 2: 1.0}, restart=0.15)

    for node_id, expected in reference.items():
        assert abs(result.nodes[node_id].ppr_personalized - expected) < 1e-10


def test_personalized_mass_sums_to_one_with_dangling() -> None:
    config = WalkConfig(max_hops=5, restart=0.15)
    result = build_walk_result(
        _similarity_by_target(_EDGES),
        seed_ids={1, 2},
        seed_weights={1: 1.0, 2: 1.0},
        config=config,
    )
    total = sum(node.ppr_personalized for node in result.nodes.values())
    assert abs(total - 1.0) < 1e-9


def test_hop_distance_and_distinct_seed_reachability() -> None:
    config = WalkConfig(max_hops=5)
    result = build_walk_result(
        _similarity_by_target(_EDGES),
        seed_ids={1, 2},
        seed_weights={1: 1.0, 2: 1.0},
        config=config,
    )
    # Node 3 is reachable only from seed 1; node 6 from both seeds at hop 2.
    assert result.nodes[3].distinct_seed_count == 1
    assert result.nodes[3].hop_distance == 1
    assert result.nodes[6].distinct_seed_count == 2
    assert result.nodes[6].hop_distance == 2


def test_bridge_node_outranks_high_degree_hub_via_lift() -> None:
    # Two weakly linked clusters. Seed 1 sits in a dense cluster A that funnels
    # into a high-degree HUB (100); seed 2 sits in cluster B. The BRIDGE (200) is
    # only weakly reachable, from one node in each cluster.
    edges: dict[int, dict[int, float]] = {
        1: {10: 0.9, 11: 0.9, 12: 0.9, 13: 0.9, 14: 0.9},
        10: {100: 0.9, 200: 0.15},
        11: {100: 0.9},
        12: {100: 0.9},
        13: {100: 0.9},
        14: {100: 0.9},
        100: {10: 0.9, 11: 0.9, 12: 0.9, 13: 0.9, 14: 0.9},  # HUB loops back: popular
        2: {20: 0.9, 21: 0.9},
        20: {2: 0.5, 200: 0.6},
        21: {2: 0.5},
        200: {},  # BRIDGE
    }
    config = WalkConfig(max_hops=4, restart=0.15, baseline_exponent=1.0)
    result = build_walk_result(
        _similarity_by_target(edges),
        seed_ids={1, 2},
        seed_weights={1: 1.0, 2: 1.0},
        config=config,
    )

    hub = result.nodes[100]
    bridge = result.nodes[200]
    # Raw personalized mass strongly favors the hub, and the hub is the more
    # central node under a walk from nowhere (higher baseline mass)...
    assert hub.ppr_personalized > bridge.ppr_personalized
    assert hub.ppr_uniform > bridge.ppr_uniform
    # ...yet the bridge wins on lift: its mass is disproportionately due to being
    # seeded from both tastes, not to raw popularity. Ranking on raw PPR would
    # pick the hub; ranking on lift picks the bridge. This is the whole point.
    assert bridge.lift > hub.lift


def test_top_paths_are_weight_ranked_and_deterministic() -> None:
    config = WalkConfig(max_hops=5)
    result = build_walk_result(
        _similarity_by_target(_EDGES),
        seed_ids={1, 2},
        seed_weights={1: 1.0, 2: 1.0},
        config=config,
    )
    paths = top_paths(result, target=6, seed_ids={1, 2}, max_paths=3)
    assert paths, "expected at least one seed->candidate path"
    # Every path starts at a seed and ends at the target.
    for path in paths:
        assert path.node_ids[0] in {1, 2}
        assert path.node_ids[-1] == 6
    # Ranked by descending path weight (product of edge weights).
    weights = [path.weight for path in paths]
    assert weights == sorted(weights, reverse=True)
    # Strongest path: 1 -> 3 -> 6 with row-normalized weights.
    assert paths[0].node_ids == (1, 3, 6)
