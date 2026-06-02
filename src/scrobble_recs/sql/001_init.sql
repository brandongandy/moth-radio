CREATE TABLE IF NOT EXISTS raw_scrobble (
    raw_scrobble_id UBIGINT,
    uts BIGINT,
    utc_time VARCHAR,
    artist VARCHAR,
    artist_mbid VARCHAR,
    album VARCHAR,
    album_mbid VARCHAR,
    track VARCHAR,
    track_mbid VARCHAR,
    imported_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS scrobble (
    scrobble_id UBIGINT,
    played_at TIMESTAMP,
    played_date DATE,
    artist_raw VARCHAR,
    album_raw VARCHAR,
    track_raw VARCHAR,
    artist_mbid_raw VARCHAR,
    album_mbid_raw VARCHAR,
    track_mbid_raw VARCHAR,
    artist_key VARCHAR,
    album_key VARCHAR,
    track_key VARCHAR,
    is_duplicate BOOLEAN DEFAULT false
);

CREATE TABLE IF NOT EXISTS artist (
    artist_id UBIGINT,
    canonical_name VARCHAR,
    artist_key VARCHAR,
    musicbrainz_artist_mbid VARCHAR,
    lastfm_name VARCHAR,
    disambiguation VARCHAR,
    country VARCHAR,
    begin_date VARCHAR,
    end_date VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS artist_listening_fact (
    artist_id UBIGINT,
    play_count BIGINT,
    track_count BIGINT,
    album_count BIGINT,
    first_played_at TIMESTAMP,
    last_played_at TIMESTAMP,
    active_days BIGINT,
    active_months BIGINT,
    session_count BIGINT,
    recent_90d_plays BIGINT,
    recent_365d_plays BIGINT,
    lifetime_weight DOUBLE,
    recent_weight DOUBLE,
    knownness DOUBLE,
    rediscovery_score DOUBLE,
    updated_at TIMESTAMP DEFAULT current_timestamp
);
