CREATE TABLE IF NOT EXISTS preference_model_artifact (
    artifact_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    query_scope VARCHAR NOT NULL,
    feature_schema_version VARCHAR NOT NULL,
    feature_names JSON NOT NULL,
    training_cutoff TIMESTAMP NOT NULL,
    coefficients JSON NOT NULL,
    intercept DOUBLE NOT NULL,
    regularization DOUBLE NOT NULL,
    training_example_count BIGINT NOT NULL,
    validation_example_count BIGINT NOT NULL,
    validation_metrics JSON NOT NULL,
    promotion_decision JSON NOT NULL,
    status VARCHAR NOT NULL,
    artifact_hash VARCHAR NOT NULL,
    training_provenance JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_lifecycle_event (
    event_id UUID PRIMARY KEY DEFAULT uuid(),
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    action VARCHAR NOT NULL,
    query_scope VARCHAR NOT NULL,
    artifact_id VARCHAR,
    details JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_promotion_evidence (
    evidence_id UUID PRIMARY KEY DEFAULT uuid(),
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    artifact_id VARCHAR NOT NULL,
    evidence JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS preference_scope_selection (
    query_scope VARCHAR PRIMARY KEY,
    suppressed BOOLEAN NOT NULL
);
