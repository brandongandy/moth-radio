CREATE TABLE IF NOT EXISTS tag_profile (
    tag VARCHAR,
    source VARCHAR,
    user_weight DOUBLE,
    artist_count BIGINT,
    top_seed_artists JSON,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS profile_cluster (
    cluster_key VARCHAR,
    cluster_name VARCHAR,
    source VARCHAR,
    tag_weights JSON,
    seed_artists JSON,
    artist_count BIGINT,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE UNIQUE INDEX IF NOT EXISTS tag_profile_natural_key
ON tag_profile (source, tag);

CREATE UNIQUE INDEX IF NOT EXISTS profile_cluster_key
ON profile_cluster (cluster_key);
