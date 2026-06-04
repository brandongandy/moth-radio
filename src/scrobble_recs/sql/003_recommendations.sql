CREATE TABLE IF NOT EXISTS recommendation_run (
    run_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    config_hash VARCHAR,
    mode VARCHAR,
    seed_summary JSON,
    scoring_weights JSON,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS recommendation (
    run_id UUID,
    rank INTEGER,
    artist_id UBIGINT,
    final_score DOUBLE,
    raw_score DOUBLE,
    lastfm_similarity_score DOUBLE,
    tag_similarity_score DOUBLE,
    cluster_fit_score DOUBLE,
    rediscovery_score DOUBLE,
    metadata_graph_score DOUBLE,
    feedback_score DOUBLE,
    knownness DOUBLE,
    obviousness DOUBLE,
    diversity_penalty DOUBLE,
    explanation JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_id_key
ON recommendation_run (run_id);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_rank_key
ON recommendation (run_id, rank);

CREATE UNIQUE INDEX IF NOT EXISTS recommendation_run_artist_key
ON recommendation (run_id, artist_id);
