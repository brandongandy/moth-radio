"""Deterministic source-family fusion contracts."""

import math

import pytest

from scrobble_recs.models.fusion import SourceCandidate, fuse_source_results


def row(artist=1, source="direct", family="collaborative", rank=1, confidence=0.1):
    return SourceCandidate(artist, source, family, rank, rank is not None, confidence)


def test_rrf_caps_after_sum_retains_membership_and_deduplicates():
    candidates = [
        row(),
        row(rank=2),
        row(source="walk"),
        row(source="radio", rank=None),
        row(source="tags", family="semantic"),
        row(artist=2, source="tags", family="semantic"),
    ]
    options = dict(
        rrf_constant=60,
        source_weights={"direct": 1, "walk": 1, "tags": 1},
        family_caps={"collaborative": 0.02, "semantic": 1},
    )
    result = fuse_source_results(candidates, **options)
    assert result == fuse_source_results(list(reversed(candidates)), **options)
    first = result[0]
    assert first.artist_id == 1
    assert first.source_contributions == {"direct": 1 / 61, "tags": 1 / 61, "walk": 1 / 61}
    assert first.family_contributions == {"collaborative": 0.02, "semantic": 1 / 61}
    assert first.fusion_score == 0.02 + 1 / 61
    assert first.independent_family_count == 2
    assert first.unranked_sources == ("radio",)


@pytest.mark.parametrize("constant", [0, -1, math.inf, math.nan])
def test_invalid_constant(constant):
    with pytest.raises(ValueError):
        fuse_source_results([], rrf_constant=constant, source_weights={}, family_caps={})


@pytest.mark.parametrize("value", [-1, math.inf, math.nan])
@pytest.mark.parametrize("field", ["source_weights", "family_caps"])
def test_invalid_weight_cap(field, value):
    options = dict(rrf_constant=60, source_weights={}, family_caps={})
    options[field] = {"direct" if field == "source_weights" else "collaborative": value}
    with pytest.raises(ValueError):
        fuse_source_results([], **options)


@pytest.mark.parametrize(
    "rank,ranked", [(None, True), (1, False), (0, True), (-1, True), (True, True), (1.5, True)]
)
def test_invalid_rank_consistency(rank, ranked):
    with pytest.raises(ValueError):
        SourceCandidate(1, "direct", "collaborative", rank, ranked, 0.5)


def test_source_family_conflicts_rejected_and_ties_by_artist():
    with pytest.raises(ValueError):
        fuse_source_results(
            [row(), row(artist=2, family="semantic")],
            rrf_constant=60,
            source_weights={"direct": 1},
            family_caps={},
        )
    result = fuse_source_results(
        [row(artist=2), row()], rrf_constant=60, source_weights={"direct": 1}, family_caps={}
    )
    assert [c.artist_id for c in result] == [1, 2]


def test_fusion_config_is_explicit_validated_reversible_and_default_off():
    from pydantic import ValidationError

    from scrobble_recs.config import RecommendationConfig, default_config_document

    defaults = RecommendationConfig()
    assert defaults.fusion.enabled is False
    assert defaults.scoring.engine == "legacy"
    assert default_config_document()["recommendation"]["fusion"]["enabled"] is False
    with pytest.raises(ValidationError, match="fusion.enabled"):
        RecommendationConfig(scoring={"engine": "fusion"})
    selected = RecommendationConfig(scoring={"engine": "fusion"}, fusion={"enabled": True})
    assert selected.fusion.source_weights.keys() == {"lastfm_similarity", "lastfm_walk", "semantic"}
    for invalid in (
        {"rrf_constant": math.nan},
        {"source_weights": {"listenbrainz": 1}},
        {"source_weights": {"semantic": -1}},
        {"family_caps": {"semantic": math.inf}},
    ):
        with pytest.raises(ValidationError):
            RecommendationConfig(fusion=invalid)


@pytest.mark.parametrize(
    "source,family",
    [
        ("lastfm_similarity", "semantic"),
        ("lastfm_walk", "catalog"),
        ("listenbrainz", "semantic"),
        ("semantic", "personal"),
    ],
)
def test_known_retriever_family_cannot_be_mislabeled(source, family):
    with pytest.raises(ValueError, match="family"):
        SourceCandidate(1, source, family, 1, True, 1)


def test_unranked_families_have_membership_and_zero_rrf():
    result = fuse_source_results(
        [
            row(source="listenbrainz", rank=None),
            row(source="musicbrainz", family="catalog", rank=None),
        ],
        rrf_constant=60,
        source_weights={"lastfm_similarity": 1},
        family_caps={"collaborative": 0.02},
    )
    assert result[0].fusion_score == 0
    assert result[0].source_contributions == result[0].family_contributions == {}
    assert result[0].independent_family_count == 2
    assert result[0].unranked_sources == ("listenbrainz", "musicbrainz")
