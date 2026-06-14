CREATE TABLE IF NOT EXISTS enrichment_queue (
    queue_id UUID PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_id UBIGINT,
    priority DOUBLE DEFAULT 0,
    status VARCHAR DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMP,
    error_message VARCHAR
);

CREATE INDEX IF NOT EXISTS enrichment_queue_source_status_key
ON enrichment_queue (source, status);

CREATE INDEX IF NOT EXISTS enrichment_queue_entity_key
ON enrichment_queue (entity_type, entity_id);
