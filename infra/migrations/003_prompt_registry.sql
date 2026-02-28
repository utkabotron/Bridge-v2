-- Prompt registry: stores current translation prompt so analytics can read it
CREATE TABLE IF NOT EXISTS prompt_registry (
    key text PRIMARY KEY,
    version text NOT NULL,
    content text NOT NULL,
    updated_at timestamptz DEFAULT now()
);
