"""Artist discovery exercises the real shared engine and local persistence."""

import json
from datetime import datetime

import pytest
from test_retrieval import stage_database as stage_database

from scrobble_recs.models.evidence import EvidenceObservation, persist_evidence_observation
from scrobble_recs.models.feedback import record_feedback_event
from scrobble_recs.models.profile_window import ListeningWindow
from scrobble_recs.models.recommend import generate_recommendations
from scrobble_recs.models.recommendation_query import RecommendationRequest

CUTOFF = datetime(2025, 1, 1)
WINDOW = ListeningWindow(datetime(2020, 1, 1), CUTOFF)


@pytest.fixture
def artist_database(stage_database):
    conn, config, _ = stage_database
    config.recommendation.calibration.enabled = False
    config.recommendation.fatigue.enabled = False
    conn.execute("UPDATE artist SET identity_confidence = .95")
    conn.execute("UPDATE artist_similarity_edge SET fetched_at = '2020-01-01'")
    conn.execute("UPDATE tag_top_artist SET fetched_at = '2020-01-01'")
    conn.execute(
        """
            INSERT INTO artist (artist_id, canonical_name, artist_key, identity_confidence)
            VALUES (5, 'Global Winner', 'global', .99), (6, 'Personal Seed', 'personal', .95)
        """
    )
    conn.execute(
        """
            INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source,
            normalized_score, fetched_at) VALUES (6, 5, 'lastfm', 1.0, '2020-01-01')
        """
    )
    conn.execute(
        """
            INSERT INTO artist_tag (artist_id, tag, source, normalized_weight, fetched_at)
            VALUES (1, 'target facet', 'lastfm', 1.0, '2020-01-01'), (4, 'target facet',
            'lastfm', 1.0, '2020-01-01'), (6, 'unrelated', 'lastfm', 1.0, '2020-01-01'), (5,
            'unrelated', 'lastfm', 1.0, '2020-01-01')
        """
    )
    conn.execute(
        """
            INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, played_date,
            is_duplicate) VALUES (1, 'zulu', 'a', '2024-01-01', '2024-01-01', false), (2,
            'personal', 'a', '2024-02-01', '2024-02-01', false), (3, 'personal', 'b',
            '2024-03-01', '2024-03-01', false), (4, 'personal', 'c', '2024-04-01', '2024-04-01',
            false)
        """
    )
    return conn, config


def run(conn, config, **kwargs):
    return generate_recommendations(
        conn,
        config,
        limit=kwargs.pop("limit", 20),
        as_of=CUTOFF,
        history_window=WINDOW,
        historical_cutoff=CUTOFF,
        local_history_end=CUTOFF,
        request=RecommendationRequest("artist", 1),
        **kwargs,
    )


def rows(conn, run_id):
    return conn.execute(
        "SELECT artist_id, rank, explanation FROM recommendation WHERE run_id = ? ORDER BY rank",
        [run_id],
    ).fetchall()


@pytest.mark.parametrize("engine", ["legacy", "walk"])
def test_artist_challenger_pool_is_target_bounded_known_labeled_new_preferred_and_persisted(
    artist_database, engine
):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = True
    config.recommendation.scoring.engine = engine
    stats = run(conn, config)
    result = rows(conn, stats.run_id)
    ids = [r[0] for r in result]
    assert set(ids) == {2, 3, 4}  # Direct neighbors and direct semantic facets only.
    assert ids.index(3) < ids.index(2)
    assert stats.recommendations < 20
    (payload,) = conn.execute(
        "SELECT query_context FROM recommendation_run WHERE run_id = ?", [stats.run_id]
    ).fetchone()
    context = json.loads(payload)
    assert context["query_kind"] == "artist"
    assert context["target_artist_id"] == 1
    assert context["target_relationship"]["state"] == "unheard"
    assert context["target_relationship"]["thresholds"]["sampled_max_plays"] == 5
    assert context["artist_settings"]["max_hops"] == 1
    candidates = conn.execute(
        """
            SELECT artist_id, known_status, feature_record, policy_effects FROM
            recommendation_candidate WHERE run_id = ? ORDER BY artist_id
        """,
        [stats.run_id],
    ).fetchall()
    assert {r[0] for r in candidates} == {2, 3, 4}
    known = next(r for r in candidates if r[0] == 2)
    assert known[1] == "known"
    features = json.loads(known[2])["values"]
    assert features["target_direct_strength"] == 0.9
    assert features["target_hop_distance"] == 1
    assert features["target_relationship_state"] == "unheard"
    assert "target_obviousness" in features
    assert features["known_status"] == "known"
    assert "target_personal_agreement" in features
    assert "artist_neighborhood" in json.loads(known[3])["reasons"]
    assert json.loads(result[0][2])["artist_query"]["target_artist_id"] == 1
    again = run(conn, config)
    assert rows(conn, again.run_id) == result


@pytest.mark.parametrize(
    ("action", "confidence"),
    [
        ("hide_artist", 0.95),
        ("reject", 0.95),
        ("less_like_this", 0.95),
        ("wrong_genre", 0.95),
        ("rediscovery_bad", 0.95),
        (None, 0.1),
    ],
)
def test_artist_neighborhood_never_restores_unsafe_neighbors(artist_database, action, confidence):
    conn, config = artist_database
    conn.execute("UPDATE artist SET identity_confidence = ? WHERE artist_id = 2", [confidence])
    if action:
        record_feedback_event(
            conn,
            run_id="00000000-0000-0000-0000-000000000001",
            artist_id=2,
            action=action,
            created_at=datetime(2020, 1, 1),
        )
    assert 2 not in [r[0] for r in rows(conn, run(conn, config).run_id)]


def observation(
    conn,
    source,
    target,
    *,
    origin=1,
    family="collaborative",
    kind="artist_radio_membership",
    payload=None,
):
    persist_evidence_observation(
        conn,
        EvidenceObservation(
            source,
            family,
            kind,
            origin,
            target,
            None,
            None,
            "easy",
            datetime(2020, 1, 1),
            0.95,
            payload or {},
        ),
        observation_key=f"{source}:{origin}:{target}",
    )


@pytest.mark.parametrize("engine", ["legacy", "walk"])
def test_artist_target_provider_memberships_are_local_unranked_and_gated(artist_database, engine):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = True
    config.recommendation.scoring.engine = engine
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    config.apis.musicbrainz.enabled = True
    config.recommendation.catalog_evidence.enabled = True
    config.recommendation.discovery_evidence.catalog_relationships_enabled = True
    config.recommendation.catalog_evidence.allowed_relationship_types = ["member of band"]
    conn.execute(
        """
            INSERT INTO artist (artist_id, canonical_name, artist_key, identity_confidence)
            VALUES (7, 'Radio', 'radio', .95), (8, 'Catalog', 'catalog', .95)
        """
    )
    observation(conn, "listenbrainz", 7)
    observation(conn, "listenbrainz", 5, origin=6)
    observation(
        conn,
        "musicbrainz",
        8,
        family="catalog",
        kind="catalog_relationship",
        payload={"relationship_type": "member of band", "supported_relationship": True},
    )
    stats = run(conn, config)
    assert {r[0] for r in rows(conn, stats.run_id)} == {2, 3, 4, 7, 8}
    for artist in [7, 8]:
        (evidence,) = conn.execute(
            "SELECT evidence FROM recommendation_candidate WHERE run_id = ? AND artist_id = ?",
            [stats.run_id, artist],
        ).fetchone()
        obs = json.loads(evidence)["observations"][0]
        assert obs["raw_score"] is None and obs["source_rank"] is None
    config.recommendation.discovery_evidence.catalog_relationships_enabled = False
    assert {r[0] for r in rows(conn, run(conn, config).run_id)} == {2, 3, 4, 7}
    config.recommendation.discovery_evidence.catalog_relationships_enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = False
    config.recommendation.catalog_evidence.allowed_relationship_types = []
    assert {r[0] for r in rows(conn, run(conn, config).run_id)} == {2, 3, 4}


def test_personal_feedback_cannot_expand_artist_neighborhood(artist_database):
    conn, config = artist_database
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=5,
        action="save",
        created_at=datetime(2020, 1, 1),
    )
    assert 5 not in [r[0] for r in rows(conn, run(conn, config).run_id)]


def test_artist_challenger_state_expands_target_walk_without_changing_scores(
    artist_database,
):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = True
    conn.execute(
        """
            INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source,
            normalized_score, fetched_at) VALUES (2, 5, 'lastfm', .8, '2020-01-01')
        """
    )
    assert 5 not in [r[0] for r in rows(conn, run(conn, config).run_id)]
    for i in range(20):
        date = datetime(2024, 1 + i % 3, 2)
        conn.execute(
            """
                INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at,
                played_date, is_duplicate) VALUES (?, 'seed', 'a', ?, ?, false)
            """,
            [100 + i, date, date.date()],
        )
    stats = run(conn, config)
    assert 5 in [r[0] for r in rows(conn, stats.run_id)]
    (payload,) = conn.execute(
        "SELECT query_context FROM recommendation_run WHERE run_id = ?", [stats.run_id]
    ).fetchone()
    context = json.loads(payload)
    assert context["artist_settings"]["max_hops"] == 2
    assert context["artist_settings"]["popularity_preference"] < 0
    assert (
        conn.execute(
            """
                SELECT normalized_score FROM artist_similarity_edge WHERE source_artist_id = 2
                AND target_artist_id = 5
            """
        ).fetchone()[0]
        == 0.8
    )


@pytest.mark.parametrize(
    "mode", ["balanced", "explore", "safe", "rediscovery", "old", "tag:target facet"]
)
def test_artist_modes_keep_target_neighborhood(artist_database, mode):
    conn, config = artist_database
    assert {r[0] for r in rows(conn, run(conn, config, mode=mode).run_id)} <= {2, 3, 4}


def test_artist_rediscovery_still_labels_known_neighbors(artist_database):
    conn, config = artist_database
    conn.execute("UPDATE scrobble SET played_at = '2021-01-01' WHERE artist_key = 'zulu'")
    conn.execute("""
        INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, is_duplicate)
        VALUES (900, 'zulu', 'a', '2021-01-01', false), (901, 'zulu', 'b', '2021-01-02', false)
    """)
    stats = run(conn, config, mode="rediscovery")
    assert conn.execute(
        """
        SELECT known_status FROM recommendation_candidate WHERE run_id = ? AND artist_id = 2
    """,
        [stats.run_id],
    ).fetchone() == ("known",)


def test_artist_snapshot_and_features_ignore_future_history_feedback_and_evidence(artist_database):
    conn, config = artist_database
    before = rows(conn, run(conn, config).run_id)
    conn.execute("""
        INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, is_duplicate)
        VALUES (800, 'alpha', 'future', '2025-01-01', false)
    """)
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=3,
        action="hide_artist",
        created_at=CUTOFF,
    )
    conn.execute("""
        INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source,
        normalized_score, fetched_at) VALUES (1, 5, 'lastfm', 1.0, '2025-01-02')
    """)
    assert rows(conn, run(conn, config).run_id) == before


def test_artist_unranked_membership_with_unsafe_provider_identity_does_not_introduce(
    artist_database,
):
    conn, config = artist_database
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    persist_evidence_observation(
        conn,
        EvidenceObservation(
            "listenbrainz",
            "collaborative",
            "artist_radio_membership",
            1,
            5,
            None,
            None,
            "easy",
            datetime(2020, 1, 1),
            0.1,
            {},
        ),
        observation_key="unsafe-radio",
    )
    assert 5 not in [r[0] for r in rows(conn, run(conn, config).run_id)]


def test_artist_recent_feedback_cooldown_survives_neighborhood_admission(artist_database):
    conn, config = artist_database
    record_feedback_event(
        conn,
        run_id="00000000-0000-0000-0000-000000000001",
        artist_id=3,
        action="save",
        created_at=datetime(2024, 12, 31),
    )
    assert 3 not in [row[0] for row in rows(conn, run(conn, config).run_id)]


@pytest.mark.parametrize("challenger", [False, True])
@pytest.mark.parametrize("cutoff_kind", ["historical", "local", "as_of"])
def test_artist_unequal_bounds_exclude_future_personal_seeds(
    artist_database, cutoff_kind, challenger
):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = challenger
    conn.execute("DELETE FROM scrobble")
    conn.execute("""
        INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source,
        normalized_score, fetched_at) VALUES (6, 3, 'lastfm', .9, '2020-01-01')
    """)
    config.recommendation.min_seed_scrobbles = 1
    cutoff = datetime(2024, 1, 1)
    options = dict(as_of=CUTOFF, historical_cutoff=CUTOFF, local_history_end=CUTOFF)
    options[
        {"historical": "historical_cutoff", "local": "local_history_end", "as_of": "as_of"}[
            cutoff_kind
        ]
    ] = cutoff

    def generate_and_read():
        stats = generate_recommendations(
            conn,
            config,
            limit=20,
            history_window=WINDOW,
            request=RecommendationRequest("artist", 1),
            **options,
        )
        context = json.loads(
            conn.execute(
                """
            SELECT query_context FROM recommendation_run WHERE run_id = ?
        """,
                [stats.run_id],
            ).fetchone()[0]
        )
        candidates = conn.execute(
            """
            SELECT artist_id, feature_record, candidate_score, relevance_score FROM
            recommendation_candidate WHERE run_id = ? ORDER BY artist_id
        """,
            [stats.run_id],
        ).fetchall()
        return (
            context["taste_region_seeds"],
            candidates,
            rows(conn, stats.run_id),
            context["history_window"],
        )

    before = generate_and_read()
    assert before[0] == []
    conn.execute("""
        INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, is_duplicate)
        VALUES (800, 'personal', 'a', '2024-06-01', false), (801, 'personal', 'b', '2024-06-02',
        false), (802, 'personal', 'c', '2024-06-03', false)
    """)
    assert generate_and_read() == before
    assert before[3]["end"] == "2024-01-01T00:00:00"


@pytest.mark.parametrize("engine", ["legacy", "walk"])
@pytest.mark.parametrize("challenger", [False, True])
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity"])
def test_artist_semantic_neighbor_ignores_nonfinite_edges_with_diagnostics(
    artist_database, engine, invalid, challenger
):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = challenger
    config.recommendation.scoring.engine = engine
    config.recommendation.calibration.enabled = True
    conn.execute(
        """
        INSERT INTO artist_similarity_edge (source_artist_id, target_artist_id, source,
        normalized_score, fetched_at) VALUES (1, 4, 'lastfm', ?::DOUBLE, '2020-01-01')
    """,
        [invalid],
    )
    stats = run(conn, config)
    context, summary = conn.execute(
        """
        SELECT query_context, seed_summary FROM recommendation_run WHERE run_id = ?
    """,
        [stats.run_id],
    ).fetchone()
    diagnostics = json.loads(summary)["retrieval_diagnostics"]["invalid_similarity_rows"]
    assert diagnostics == [
        {
            "source_artist_id": 1,
            "target_artist_id": 4,
            "reason": "nonfinite_similarity",
            "value": invalid.lower(),
        }
    ]
    candidate = conn.execute(
        """
        SELECT max_source_score, feature_record, evidence, quality_gate FROM
        recommendation_candidate WHERE run_id = ? AND artist_id = 4
    """,
        [stats.run_id],
    ).fetchone()
    assert candidate is not None
    assert candidate[0] == 0.0
    features = json.loads(candidate[1])["values"]
    assert features["target_direct_strength"] == 0.0
    assert features["lastfm_similarity_score"] == 0.0

    def finite_json(payload):
        return json.loads(
            payload, parse_constant=lambda value: pytest.fail(f"nonfinite JSON: {value}")
        )

    for payload in [context, summary, *candidate[1:]]:
        finite_json(payload)
    assert all(
        obs["relationship_kind"] != "similar_artist"
        for obs in json.loads(candidate[2])["observations"]
    )


@pytest.mark.parametrize("limit", [1, 20])
@pytest.mark.parametrize("diversity", [False, True])
def test_artist_policy_effects_are_separate_from_relevance_and_accounted(
    artist_database, limit, diversity
):
    conn, config = artist_database
    config.recommendation.artist_challenger.enabled = True
    config.recommendation.obviousness.enabled = True
    conn.execute("""
        INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank, fetched_at) VALUES (2,
        'target facet', 'lastfm', 2, '2020-01-01'), (3, 'target facet', 'lastfm', 2,
        '2020-01-01')
    """)
    config.recommendation.diversity.enabled = diversity
    stats = run(conn, config, limit=limit)
    persisted = conn.execute(
        """
        SELECT artist_id, candidate_score, relevance_score, policy_adjustment, policy_effects
        FROM recommendation_candidate WHERE run_id = ? ORDER BY artist_id
    """,
        [stats.run_id],
    ).fetchall()
    known, new = persisted[:2]
    assert known[1] == pytest.approx(new[1])
    assert known[2] == pytest.approx(new[2])
    for artist, _candidate, relevance, adjustment, payload in persisted:
        effects = json.loads(payload)
        assert relevance + sum(effects["components"].values()) == pytest.approx(
            effects["final_score"]
        )
        assert adjustment == pytest.approx(sum(effects["components"].values()))
        if artist == 2:
            assert effects["components"]["known_neighbor_novelty"] < 0
            assert "known_neighbor_novelty" in effects["reasons"]
        if artist in (2, 3):
            assert effects["components"]["relationship_popularity"] > 0
            assert "relationship_popularity" in effects["reasons"]


@pytest.mark.parametrize("engine", ["legacy", "walk"])
def test_default_artist_profile_uses_existing_scorer_controls_and_can_be_restored(
    artist_database, engine
):
    conn, config = artist_database
    config.recommendation.scoring.engine = engine
    stats = run(conn, config)
    payload = json.loads(
        conn.execute(
            """
        SELECT query_context FROM recommendation_run WHERE run_id = ?
    """,
            [stats.run_id],
        ).fetchone()[0]
    )
    assert payload["artist_settings"]["profile"] == "control"
    assert payload["artist_settings"]["version"] == "artist-control-v1"
    assert payload["artist_settings"]["max_hops"] == config.recommendation.walk.max_hops
    assert payload["artist_settings"]["known_neighbor_penalty"] == 0
    assert payload["artist_settings"]["popularity_preference"] == 0
    assert (
        payload["artist_settings"]["obviousness_penalty_weight"]
        == config.recommendation.obviousness.penalty_weight
    )
    records = conn.execute(
        """
        SELECT artist_id, candidate_score, feature_record FROM recommendation_candidate WHERE
        run_id = ? ORDER BY artist_id
    """,
        [stats.run_id],
    ).fetchall()
    for _artist, candidate_score, features in records:
        values = json.loads(features)["values"]
        expected = sum(
            config.recommendation.weights.model_dump()[key] * values["calibrated_" + key]
            for key in ("lastfm_similarity", "tag_similarity", "feedback_model")
            if key != "feedback_model"
        )
        expected += config.recommendation.weights.feedback_model * values["calibrated_feedback"]
        expected += config.recommendation.weights.rediscovery * values["rediscovery_score"]
        assert candidate_score == pytest.approx(expected)
        assert values["artist_target_contribution"] == values["artist_personal_contribution"] == 0
        assert values["artist_existing_contribution"] == pytest.approx(candidate_score)
    before = rows(conn, stats.run_id)
    config.recommendation.artist_challenger.enabled = True
    challenger = run(conn, config)
    activated = json.loads(
        conn.execute(
            """
        SELECT query_context FROM recommendation_run WHERE run_id = ?
    """,
            [challenger.run_id],
        ).fetchone()[0]
    )["artist_settings"]
    assert activated["profile"] == "challenger"
    assert activated["target_weight"] == 0.75
    assert activated["structural_membership_prior"] == 0.25
    assert rows(conn, challenger.run_id) != before
    config.recommendation.artist_challenger.enabled = False
    assert rows(conn, run(conn, config).run_id) == before


def test_unranked_membership_has_no_default_positive_prior(artist_database):
    conn, config = artist_database
    config.apis.listenbrainz.enabled = True
    config.apis.listenbrainz.candidate_generation_enabled = True
    observation(conn, "listenbrainz", 5)
    stats = run(conn, config)
    assert 5 not in [row[0] for row in rows(conn, stats.run_id)]
    candidate = conn.execute(
        """
        SELECT candidate_score, feature_record FROM recommendation_candidate WHERE run_id = ?
        AND artist_id = 5
    """,
        [stats.run_id],
    ).fetchone()
    assert candidate[0] == 0
    assert json.loads(candidate[1])["values"]["artist_target_contribution"] == 0
    effects = json.loads(
        conn.execute(
            "SELECT policy_effects FROM recommendation_candidate "
            "WHERE run_id = ? AND artist_id = 5",
            [stats.run_id],
        ).fetchone()[0]
    )
    assert "nonpositive_policy_score" in effects["reasons"]


def test_default_artist_slate_uses_existing_knownness_preference(artist_database):
    conn, config = artist_database
    conn.execute("""
        INSERT INTO scrobble (scrobble_id, artist_key, track_key, played_at, is_duplicate)
        SELECT 1000+i, 'zulu', 'track', TIMESTAMP '2024-01-01' + (i%12)*INTERVAL 1 MONTH, false
        FROM generate_series(1, 60) AS plays(i)
    """)
    stats = run(conn, config)
    ranked = [row[0] for row in rows(conn, stats.run_id)]
    assert ranked.index(3) < ranked.index(2)
    candidate, relevance, policy = conn.execute(
        """
        SELECT candidate_score, relevance_score, policy_effects FROM recommendation_candidate
        WHERE run_id = ? AND artist_id = 2
    """,
        [stats.run_id],
    ).fetchone()
    effects = json.loads(policy)["components"]
    assert effects["knownness"] < 0
    assert effects["known_neighbor_novelty"] == 0
    assert effects["relationship_popularity"] == 0
    assert relevance + sum(effects.values()) == pytest.approx(
        next(
            row[0]
            for row in conn.execute(
                """
        SELECT final_score FROM recommendation WHERE run_id = ? AND artist_id = 2
    """,
                [stats.run_id],
            ).fetchall()
        )
    )


@pytest.mark.parametrize("policy_enabled", [False, True])
def test_artist_challenger_persists_actual_tunables_contributions_and_enabled_effects(
    artist_database, policy_enabled
):
    conn, config = artist_database
    challenger = config.recommendation.artist_challenger
    challenger.enabled = True
    challenger.target_weight, challenger.personal_weight, challenger.existing_weight = 0.6, 0.2, 0.2
    challenger.structural_membership_prior = 0.4
    challenger.hop_proximity_scale = 0.4
    challenger.known_neighbor_penalty = 0.3
    challenger.unheard.popularity_preference = 0.2
    challenger.unheard.obviousness_penalty_weight = 0.1
    config.recommendation.obviousness.enabled = policy_enabled
    config.recommendation.knownness.enabled = policy_enabled
    conn.execute("""
        INSERT INTO tag_top_artist (artist_id, tag, source, tag_rank, fetched_at) VALUES (2,
        'target facet', 'lastfm', 2, '2020-01-01')
    """)
    stats = run(conn, config)
    settings = json.loads(
        conn.execute(
            """
        SELECT query_context FROM recommendation_run WHERE run_id = ?
    """,
            [stats.run_id],
        ).fetchone()[0]
    )["artist_settings"]
    assert settings["profile"] == "challenger"
    assert (
        settings["target_weight"] == 0.6
        and settings["personal_weight"] == 0.2
        and settings["existing_weight"] == 0.2
    )
    assert settings["structural_membership_prior"] == settings["hop_proximity_scale"] == 0.4
    assert (
        settings["popularity_preference"] == 0.2 and settings["obviousness_penalty_weight"] == 0.1
    )
    candidate, relevance, payload, effects_json = conn.execute(
        """
        SELECT candidate_score, relevance_score, feature_record, policy_effects FROM
        recommendation_candidate WHERE run_id = ? AND artist_id = 2
    """,
        [stats.run_id],
    ).fetchone()
    values, effects = json.loads(payload)["values"], json.loads(effects_json)
    assert candidate == pytest.approx(
        sum(
            values[key]
            for key in (
                "artist_target_contribution",
                "artist_personal_contribution",
                "artist_existing_contribution",
            )
        )
    )
    assert values["artist_target_contribution"] == pytest.approx(0.6 * values["target_relevance"])
    assert values["artist_personal_contribution"] == pytest.approx(
        0.2 * values["target_personal_agreement"]
    )
    assert values["artist_existing_contribution"] == pytest.approx(
        0.2 * values["artist_existing_raw_score"]
    )
    assert relevance + sum(effects["components"].values()) == pytest.approx(effects["final_score"])
    if policy_enabled:
        assert effects["components"]["known_neighbor_novelty"] == pytest.approx(-0.3 * 0.975)
        assert effects["components"]["relationship_popularity"] == pytest.approx(0.2 * 0.975)
    else:
        assert (
            effects["components"]["known_neighbor_novelty"]
            == effects["components"]["relationship_popularity"]
            == 0
        )
