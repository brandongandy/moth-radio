CREATE TABLE IF NOT EXISTS artist_evidence_observation (
    observation_id UUID PRIMARY KEY,
    source VARCHAR NOT NULL,
    family VARCHAR NOT NULL,
    relationship_kind VARCHAR NOT NULL,
    observation_key VARCHAR NOT NULL,
    source_artist_id UBIGINT,
    target_artist_id UBIGINT NOT NULL,
    raw_score DOUBLE,
    source_rank INTEGER,
    provider_mode VARCHAR,
    fetched_at TIMESTAMP,
    cache_key VARCHAR,
    payload_hash VARCHAR,
    identity_status VARCHAR,
    identity_confidence DOUBLE NOT NULL,
    evidence JSON NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS artist_evidence_natural_key
ON artist_evidence_observation (source, relationship_kind, observation_key);
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS evidence JSON;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS feature_record JSON;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS admission JSON;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS policy_effects JSON;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS candidate_score DOUBLE;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS relevance_score DOUBLE;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS policy_adjustment DOUBLE;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS known_status VARCHAR;
ALTER TABLE recommendation_candidate ADD COLUMN IF NOT EXISTS backoff_stage INTEGER;
