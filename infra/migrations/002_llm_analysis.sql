-- Migration 002: LLM nightly analysis tables
-- Tables for automated problem detection and translation quality evaluation

CREATE TABLE IF NOT EXISTS nightly_analysis_runs (
    id              bigserial PRIMARY KEY,
    run_date        date NOT NULL,
    flow_type       text NOT NULL CHECK (flow_type IN ('problems', 'translation_quality')),
    summary         jsonb,
    tokens_used     int DEFAULT 0,
    estimated_cost  numeric(10, 6) DEFAULT 0,
    created_at      timestamptz DEFAULT now(),
    UNIQUE (run_date, flow_type)
);

CREATE TABLE IF NOT EXISTS detected_issues (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES nightly_analysis_runs(id) ON DELETE CASCADE,
    severity        text NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
    category        text NOT NULL,
    title           text NOT NULL,
    description     text,
    suggested_fix   text,
    acknowledged    boolean DEFAULT false,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_detected_issues_run_id ON detected_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_detected_issues_severity ON detected_issues(severity);

CREATE TABLE IF NOT EXISTS translation_evaluations (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES nightly_analysis_runs(id) ON DELETE CASCADE,
    message_event_id bigint REFERENCES message_events(id) ON DELETE SET NULL,
    original_text   text,
    translated_text text,
    quality_score   smallint CHECK (quality_score BETWEEN 1 AND 5),
    accuracy_score  smallint CHECK (accuracy_score BETWEEN 1 AND 5),
    naturalness_score smallint CHECK (naturalness_score BETWEEN 1 AND 5),
    issues_found    jsonb DEFAULT '[]'::jsonb,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_translation_evaluations_run_id ON translation_evaluations(run_id);

CREATE TABLE IF NOT EXISTS prompt_suggestions (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES nightly_analysis_runs(id) ON DELETE CASCADE,
    suggestion      text NOT NULL,
    rationale       text,
    status          text DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected')),
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prompt_suggestions_run_id ON prompt_suggestions(run_id);
