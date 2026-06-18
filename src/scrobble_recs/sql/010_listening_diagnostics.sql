ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS local_scrobble_count INTEGER;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS listened_day_count INTEGER;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS first_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS last_listened_at TIMESTAMP;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS years_since_last_listen DOUBLE;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS rediscovery_eligible BOOLEAN;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS trial_penalty DOUBLE DEFAULT 0.0;
