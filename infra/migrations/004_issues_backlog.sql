-- Issues backlog: persistent storage for critical issues across nightly runs.
-- detected_issues are overwritten each run (DELETE + INSERT), so critical
-- issues would be lost after a day. This table preserves them for review.

CREATE TABLE IF NOT EXISTS issues_backlog (
    id bigserial PRIMARY KEY,
    source_run_date date NOT NULL,
    severity text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    description text,
    suggested_fix text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'wontfix')),
    resolved_at timestamptz,
    created_at timestamptz DEFAULT now()
);

CREATE INDEX idx_issues_backlog_status ON issues_backlog(status);
