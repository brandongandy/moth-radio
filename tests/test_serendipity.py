import pytest

from scrobble_recs.config import SerendipityConfig
from scrobble_recs.models.serendipity import score_serendipity


def _serendipity() -> SerendipityConfig:
    return SerendipityConfig()


def test_relevant_cross_cluster_candidate_gets_bridge_bonus() -> None:
    graph = {1: {2: 0.9}, 2: {1: 0.9}, 3: {4: 0.9}, 4: {3: 0.9}}

    result = score_serendipity(
        candidate_edges=((1, 0.8), (3, 0.8)),
        seed_graph=graph,
        config=_serendipity(),
    )

    assert result.is_bridge is True
    assert result.cohesion == 0.0
    assert result.raw_bonus > 0.0


def test_deeply_clustered_candidate_gets_smaller_bonus() -> None:
    graph = {1: {2: 0.9}, 2: {1: 0.9}}

    result = score_serendipity(
        candidate_edges=((1, 0.8), (2, 0.8)),
        seed_graph=graph,
        config=_serendipity(),
    )

    assert result.is_bridge is False
    assert result.cohesion == pytest.approx(0.9)
    assert result.raw_bonus < _serendipity().max_bonus


def test_single_neighbor_never_receives_serendipity_bonus() -> None:
    result = score_serendipity(
        candidate_edges=((1, 0.8),),
        seed_graph={},
        config=_serendipity(),
    )

    assert result.raw_bonus == 0.0
    assert result.calibrated_bonus == 0.0


def test_duplicate_edges_for_one_seed_remain_one_neighbor_before_minimum_check() -> None:
    result = score_serendipity(
        candidate_edges=((7, 0.80), (7, 0.95)),
        seed_graph={},
        config=_serendipity(),
    )

    assert result.neighbors == (7,)
    assert result.raw_bonus == 0.0


def test_candidate_neighbors_are_sorted_deterministically() -> None:
    result = score_serendipity(
        candidate_edges=((9, 0.80), (3, 0.80), (9, 0.95)),
        seed_graph={3: {}, 9: {}},
        config=_serendipity(),
    )

    assert result.neighbors == (3, 9)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"minimum_neighbor_edge": 1.01}, "minimum_neighbor_edge must be between 0 and 1"),
        ({"bridge_bonus": 0.16}, "bridge_bonus must be <= max_bonus"),
    ],
)
def test_serendipity_config_rejects_invalid_bonus_settings(
    kwargs: dict[str, float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SerendipityConfig(**kwargs)
