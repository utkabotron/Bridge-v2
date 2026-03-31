-- Feature flags for runtime control without deploy
CREATE TABLE IF NOT EXISTS feature_flags (
    name TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed default flags
INSERT INTO feature_flags (name, enabled) VALUES
    ('media_analysis_enabled', true),
    ('translation_enabled', true),
    ('direct_chat_enabled', true),
    ('admin_alerts_enabled', true)
ON CONFLICT (name) DO NOTHING;
