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
