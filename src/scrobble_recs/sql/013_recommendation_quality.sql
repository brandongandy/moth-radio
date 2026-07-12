ALTER TABLE recommendation_candidate
ADD COLUMN IF NOT EXISTS eligibility_state VARCHAR;

ALTER TABLE recommendation_candidate
ADD COLUMN IF NOT EXISTS eligibility_reasons JSON;

ALTER TABLE recommendation_candidate
ADD COLUMN IF NOT EXISTS quality_gate JSON;
