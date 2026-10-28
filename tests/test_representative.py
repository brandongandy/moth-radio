"""Recording preference never participates in artist admission or scoring."""

from dataclasses import replace
from datetime import datetime
from itertools import permutations

import pytest

from scrobble_recs.apis.musicbrainz import MusicBrainzRecording
from scrobble_recs.models.evidence import EvidenceObservation

ARTIST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SEED = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REC = "10000000-0000-4000-8000-000000000001"
REC2 = "10000000-0000-4000-8000-000000000002"


def radio(*, recordings=None, fetched=None, **updates):
    evidence = {
        "artist_mbid": ARTIST,
        "identity_warnings": [],
        "cache_key": "radio-key",
        "payload_hash": "radio-hash",
        "seed_artist_mbid": SEED,
        "mode": "easy",
        "max_similar_artists": 100,
        "max_recordings_per_artist": 3,
        "pop_begin": 0,
        "pop_end": 100,
        "recordings": recordings
        if recordings is not None
        else [{"recording_mbid": REC, "total_listen_count": 5000}],
    }
    evidence.update(updates)
    return EvidenceObservation(
        "listenbrainz",
        "collaborative",
        "artist_radio_membership",
        1,
        2,
        None,
        None,
        "easy",
        fetched or datetime(2026, 1, 1),
        0.9,
        evidence,
    )


def metadata(identifier=REC, **updates):
    return replace(
        MusicBrainzRecording(identifier, "Literal [雪]", (ARTIST, OTHER), None, None), **updates
    )


def select(observations, recordings=None, artist=ARTIST):
    from scrobble_recs.models.representative import select_start_here_item

    return select_start_here_item(
        recommended_artist_mbid=artist,
        observations=observations,
        recording_metadata=recordings if recordings is not None else {REC: metadata()},
    )


def test_selection_uses_corroborated_radio_count_not_percentile_and_copies_provenance():
    observation = radio()
    item = select([observation])
    assert item.kind == "recording"
    assert item.title == "Literal [雪]"
    assert item.musicbrainz_id == REC
    assert item.source == "listenbrainz"
    assert item.evidence["listenbrainz"]["total_listen_count"] == 5000
    assert item.evidence["musicbrainz"]["recording"]["artist_mbids"] == [ARTIST, OTHER]
    assert "MusicBrainz" in item.reason and "listen" in item.reason.lower()
    item.evidence["listenbrainz"]["identity_warnings"].append("changed")
    assert observation.evidence["identity_warnings"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"pop_begin": None},
        {"pop_end": 101},
        {"pop_begin": True},
        {"mode": "hard"},
        {"max_recordings_per_artist": False},
        {"seed_artist_mbid": "bad"},
        {"cache_key": ""},
        {"payload_hash": None},
        {"identity_warnings": "safe"},
        {"identity_warnings": [3]},
        {"artist_mbid": "bad"},
    ],
)
def test_selection_requires_inspectable_safe_radio_provenance(updates):
    assert select([radio(**updates)]) is None


@pytest.mark.parametrize("count", [True, "5", 1.5, -1, None, float("nan"), float("inf")])
def test_selection_rejects_unusable_counts(count):
    assert (
        select([radio(recordings=[{"recording_mbid": REC, "total_listen_count": count}])]) is None
    )


def test_selection_count_tie_and_permutations_are_deterministic():
    older = radio(recordings=[{"recording_mbid": REC, "total_listen_count": 999}])
    entries = [
        {"recording_mbid": REC2, "total_listen_count": 0},
        {"recording_mbid": REC.upper(), "total_listen_count": 0},
    ]
    for order in permutations(entries):
        newest = radio(recordings=list(order), fetched=datetime(2026, 2, 1))
        for observations in permutations([older, newest, newest]):
            item = select(observations, {REC2: metadata(REC2), REC: metadata()})
            assert item.musicbrainz_id == REC
            assert item.evidence["listenbrainz"]["total_listen_count"] == 0


def test_selection_conflicting_counts_discard_only_affected_recording():
    first = radio(
        recordings=[
            {"recording_mbid": REC, "total_listen_count": 9},
            {"recording_mbid": REC2, "total_listen_count": 1},
        ]
    )
    second = radio(recordings=[{"recording_mbid": REC, "total_listen_count": 10}])
    assert select([first, second], {REC: metadata(), REC2: metadata(REC2)}).musicbrainz_id == REC2
    assert (
        select(
            [radio(recordings=first.evidence["recordings"] + second.evidence["recordings"])],
            {REC: metadata(), REC2: metadata(REC2)},
        ).musicbrainz_id
        == REC2
    )


@pytest.mark.parametrize(
    "conflict",
    [
        radio(artist_mbid=OTHER),
        radio(identity_warnings=["AMBIGUOUS target"]),
        radio(identity_warnings=["name_conflict"]),
        radio(identity_warnings=["multiple_matches"]),
        replace(radio(), target_artist_id=3),
    ],
)
def test_selection_does_not_cherry_pick_safe_observation(conflict):
    assert select([radio(), conflict]) is None


@pytest.mark.parametrize(
    "recording",
    [
        metadata(artist_mbids=(OTHER,)),
        metadata(artist_mbids=(ARTIST, "bad")),
        metadata(mbid=REC2),
        metadata(title=" "),
        metadata(release_mbid="bad"),
    ],
)
def test_selection_revalidates_metadata_models(recording):
    assert select([radio()], {REC: recording}) is None


def test_selection_metadata_aliases_collapse_or_conflict_without_first_entry_bias():
    for entries in permutations(
        [(REC, metadata()), ("{" + REC + "}", metadata(title="Different"))]
    ):
        assert select([radio()], dict(entries)) is None
    assert select([radio()], {REC: metadata(), "{" + REC + "}": metadata()}).musicbrainz_id == REC
    assert select([radio()], {}) is None
    assert select([replace(radio(), identity_confidence=0.89)]) is None
    assert select([replace(radio(), source="lastfm")]) is None
    assert select([replace(radio(), fetched_at=None)]) is None
    assert select([replace(radio(), source_artist_id=None)]) is None
    assert select([radio()], artist="bad") is None
    assert select([]) is None


def test_selection_prefers_larger_count_and_canonicalizes_alias_credits():
    observation = radio(
        recordings=[
            {"recording_mbid": REC, "total_listen_count": 1},
            {"recording_mbid": REC2, "total_listen_count": 500000},
        ]
    )
    item = select(
        [observation], {REC: metadata(), REC2: metadata(REC2, artist_mbids=(ARTIST.upper(),))}
    )
    assert item.musicbrainz_id == REC2
    assert item.evidence["musicbrainz"]["recording"]["artist_mbids"] == [ARTIST]


def test_selection_conflicting_metadata_release_pairs_and_stable_radio_provenance():
    assert (
        select(
            [radio()],
            {REC: metadata(), "{" + REC + "}": metadata(release_mbid=OTHER, release_title="Album")},
        )
        is None
    )
    first, second = radio(cache_key="a-key"), radio(cache_key="z-key")
    assert select([second, first]).evidence["listenbrainz"]["cache_key"] == "a-key"
    assert select([first, second]) == select([second, first])
    assert select([replace(first, family="semantic")]) is None
    assert select([replace(first, relationship_kind="similar_artist")]) is None
