-- 008: Direct interactions tracking (translations + media analysis from bot private chat)

CREATE TABLE IF NOT EXISTS public.direct_interactions (
    id              bigserial PRIMARY KEY,
    user_id         bigint REFERENCES public.users(id) ON DELETE CASCADE,
    interaction_type varchar(20) NOT NULL CHECK (interaction_type IN ('translation', 'media_analysis')),
    -- Translation fields
    original_text   text,
    translated_text text,
    target_language text,
    translation_ms  integer,
    cache_hit       boolean,
    -- Media analysis fields
    analysis_type   varchar(20),
    media_mime_type text,
    media_filename  text,
    result_text     text,
    processing_ms   integer,
    -- Common
    status          varchar(20) NOT NULL DEFAULT 'completed' CHECK (status IN ('completed', 'failed')),
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_di_user ON direct_interactions(user_id);
CREATE INDEX idx_di_created ON direct_interactions(created_at);
