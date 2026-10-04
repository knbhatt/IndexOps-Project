CREATE TABLE IF NOT EXISTS tickets (
    ticket_id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    category VARCHAR(50),
    priority VARCHAR(20),
    assigned_team VARCHAR(50),
    status VARCHAR(20) DEFAULT 'open',
    resolution_notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id SERIAL PRIMARY KEY,
    dag_run_id VARCHAR(100),
    status VARCHAR(20),
    inject_type VARCHAR(50) DEFAULT 'none',
    inject_pct FLOAT DEFAULT 0,
    started_at TIMESTAMP,
    ended_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS data_quality_metrics (
    metric_id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    total_processed INTEGER,
    total_indexed INTEGER,
    mismatch_count INTEGER,
    mismatch_pct FLOAT,
    duplicate_count INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS index_metrics (
    metric_id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    index_name VARCHAR(100),
    doc_count INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    severity VARCHAR(20),
    message TEXT,
    status VARCHAR(20) DEFAULT 'open',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id SERIAL PRIMARY KEY,
    alert_id INTEGER REFERENCES alerts(alert_id),
    run_id INTEGER REFERENCES pipeline_runs(run_id),
    status VARCHAR(20) DEFAULT 'investigating',
    root_cause TEXT,
    confidence FLOAT,
    recommended_action TEXT,
    evidence JSONB,
    report JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS remediation_actions (
    action_id SERIAL PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(incident_id),
    action_taken TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    params JSONB,
    records_fixed INTEGER,
    records_failed INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS investigation_steps (
    step_id SERIAL PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(incident_id),
    tool_name VARCHAR(100),
    tool_input JSONB,
    tool_output JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS validation_passed BOOLEAN;
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS validation_notes TEXT;