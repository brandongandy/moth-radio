ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS query_kind VARCHAR DEFAULT 'general';
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS target_artist_id UBIGINT;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS query_context JSON;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS evidence_snapshot_id VARCHAR;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS feature_schema_version VARCHAR;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS fusion_version VARCHAR;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS ranker_version VARCHAR;
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS slate_policy_version VARCHAR;
