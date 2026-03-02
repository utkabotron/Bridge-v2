-- 005_weekly_insights.sql
-- Weekly intelligence memory + analytics changelog

CREATE TABLE IF NOT EXISTS weekly_insights (
    id SERIAL PRIMARY KEY,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    executive_summary TEXT,
    deep_analysis JSONB,
    recommendations JSONB,
    prompt_draft TEXT,
    analytics_meta JSONB,
    previous_recommendations_review JSONB,
    tokens_used INT DEFAULT 0,
    estimated_cost NUMERIC(10,4) DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(week_start)
);

CREATE TABLE IF NOT EXISTS analytics_changelog (
    id SERIAL PRIMARY KEY,
    change_date DATE NOT NULL DEFAULT current_date,
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    impact_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Initial changelog entry for model switch
INSERT INTO analytics_changelog (change_type, description, impact_notes)
VALUES (
    'model_switch',
    'Switched daily flows (nightly-problems, translation-quality) from o3-mini to gpt-4.1-mini. Switched weekly-report from gpt-4o-mini to o3.',
    'Daily: lower cost (~10x cheaper), comparable quality for evaluation tasks. Weekly: much deeper analysis with o3 thinking model + persistent memory via weekly_insights table.'
);
