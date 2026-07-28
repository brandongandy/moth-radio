-- Walk scorer: additive, versioned. Legacy runs default to 'legacy' and NULL
-- hop distance, so they continue to export and render unchanged.
ALTER TABLE recommendation_run ADD COLUMN IF NOT EXISTS scoring_engine VARCHAR DEFAULT 'legacy';
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS hop_distance INTEGER;
