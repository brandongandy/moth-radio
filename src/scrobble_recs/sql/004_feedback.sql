CREATE TABLE IF NOT EXISTS feedback_event (
    feedback_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    run_id UUID,
    artist_id UBIGINT,
    action VARCHAR,
    value DOUBLE,
    context JSON,
    note VARCHAR
);

CREATE UNIQUE INDEX IF NOT EXISTS feedback_event_id_key
ON feedback_event (feedback_id);

CREATE INDEX IF NOT EXISTS feedback_event_run_key
ON feedback_event (run_id);

CREATE INDEX IF NOT EXISTS feedback_event_artist_key
ON feedback_event (artist_id);

CREATE INDEX IF NOT EXISTS feedback_event_action_key
ON feedback_event (action);
