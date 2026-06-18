ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE DEFAULT 0.0;
ALTER TABLE artist ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR;

ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS candidate_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS canonical_artist_name VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_artist_mbid VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS lastfm_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS musicbrainz_url VARCHAR;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE;
ALTER TABLE recommendation ADD COLUMN IF NOT EXISTS identity_warnings VARCHAR;
