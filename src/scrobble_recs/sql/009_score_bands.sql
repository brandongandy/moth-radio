ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS score_band VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS evidence_source_count INTEGER DEFAULT 0;
