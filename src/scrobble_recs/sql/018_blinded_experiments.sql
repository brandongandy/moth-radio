CREATE TABLE IF NOT EXISTS recommendation_experiment (
    experiment_id UUID PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    task VARCHAR,
    control_run_id UUID,
    challenger_run_id UUID,
    evidence_snapshot_id VARCHAR,
    randomization_seed BIGINT,
    definition JSON,
    status VARCHAR
);
CREATE TABLE IF NOT EXISTS recommendation_exposure (
    exposure_id UUID PRIMARY KEY,
    experiment_id UUID NOT NULL,
    created_at TIMESTAMP NOT NULL,
    context JSON NOT NULL,
    shown_items JSON NOT NULL,
    attribution JSON NOT NULL,
    completed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS recommendation_judgment (
    judgment_id UUID PRIMARY KEY,
    exposure_id UUID NOT NULL,
    created_at TIMESTAMP NOT NULL,
    preferred_side VARCHAR,
    list_choice VARCHAR,
    best_discovery_artist_id UBIGINT,
    labels JSON NOT NULL,
    confidence DOUBLE,
    note VARCHAR
);
CREATE TABLE IF NOT EXISTS recommendation_downstream_outcome (
    exposure_id UUID NOT NULL,
    artist_id UBIGINT NOT NULL,
    first_listened_at TIMESTAMP,
    play_count BIGINT NOT NULL,
    active_days BIGINT NOT NULL,
    outcome_cutoff TIMESTAMP NOT NULL,
    PRIMARY KEY (exposure_id, artist_id)
);
