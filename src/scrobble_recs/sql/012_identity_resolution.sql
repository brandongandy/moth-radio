CREATE TABLE IF NOT EXISTS artist_identity_resolution (
    artist_id UBIGINT,
    source VARCHAR,
    external_id VARCHAR,
    canonical_name VARCHAR,
    disambiguation VARCHAR,
    country_or_area VARCHAR,
    type VARCHAR,
    confidence DOUBLE,
    status VARCHAR,
    warning_codes VARCHAR,
    resolved_at TIMESTAMP DEFAULT current_timestamp,
    raw_cache_key VARCHAR
);

CREATE UNIQUE INDEX IF NOT EXISTS artist_identity_resolution_key
ON artist_identity_resolution (artist_id, source);
