CREATE TABLE IF NOT EXISTS recommendation_candidate (
    run_id UUID,
    artist_id UBIGINT,
    candidate_sources JSON,
    max_source_score DOUBLE,
    appeared_from_seed_count INTEGER,
    appeared_from_tag_count INTEGER,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, artist_id)
);
