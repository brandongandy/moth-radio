-- Empty locale is the canonical missing-locale value, so uniqueness includes it.
CREATE TABLE IF NOT EXISTS artist_alias (
    artist_id BIGINT NOT NULL,
    alias VARCHAR NOT NULL,
    normalized_alias VARCHAR NOT NULL,
    locale VARCHAR NOT NULL DEFAULT '',
    primary_for_locale BOOLEAN NOT NULL DEFAULT false,
    begin_date VARCHAR,
    end_date VARCHAR,
    type VARCHAR,
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMP NOT NULL,
    UNIQUE (source, artist_id, normalized_alias, locale)
);
