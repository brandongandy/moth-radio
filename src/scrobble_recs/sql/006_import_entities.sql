CREATE TABLE IF NOT EXISTS track (
    track_id UBIGINT,
    artist_id UBIGINT,
    canonical_title VARCHAR,
    track_key VARCHAR,
    musicbrainz_recording_mbid VARCHAR,
    lastfm_track_name VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS import_warning (
    warning_id UBIGINT,
    import_row_number UBIGINT,
    warning_code VARCHAR,
    severity VARCHAR,
    message VARCHAR,
    evidence JSON,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS track_artist_key_natural_key
ON track (artist_id, track_key);

CREATE UNIQUE INDEX IF NOT EXISTS import_warning_id_key
ON import_warning (warning_id);

CREATE INDEX IF NOT EXISTS import_warning_code_key
ON import_warning (warning_code);

CREATE INDEX IF NOT EXISTS import_warning_row_key
ON import_warning (import_row_number);
