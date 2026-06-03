CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR PRIMARY KEY,
    source VARCHAR,
    endpoint VARCHAR,
    request_url VARCHAR,
    request_params JSON,
    response_status INTEGER,
    response_json JSON,
    fetched_at TIMESTAMP,
    expires_at TIMESTAMP,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS enrichment_work (
    source VARCHAR,
    endpoint VARCHAR,
    subject_key VARCHAR,
    request_limit INTEGER,
    completed_at TIMESTAMP,
    evidence JSON
);

CREATE TABLE IF NOT EXISTS artist_similarity_edge (
    source_artist_id UBIGINT,
    target_artist_id UBIGINT,
    source VARCHAR,
    source_score DOUBLE,
    normalized_score DOUBLE,
    evidence JSON,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_tag (
    artist_id UBIGINT,
    tag VARCHAR,
    source VARCHAR,
    tag_count DOUBLE,
    normalized_weight DOUBLE,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tag_top_artist (
    tag VARCHAR,
    artist_id UBIGINT,
    source VARCHAR,
    tag_rank INTEGER,
    tag_count DOUBLE,
    fetched_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS enrichment_work_natural_key
ON enrichment_work (source, endpoint, subject_key, request_limit);

CREATE UNIQUE INDEX IF NOT EXISTS artist_similarity_edge_natural_key
ON artist_similarity_edge (source, source_artist_id, target_artist_id);

CREATE UNIQUE INDEX IF NOT EXISTS artist_tag_natural_key
ON artist_tag (source, artist_id, tag);

CREATE UNIQUE INDEX IF NOT EXISTS tag_top_artist_natural_key
ON tag_top_artist (source, tag, artist_id);
