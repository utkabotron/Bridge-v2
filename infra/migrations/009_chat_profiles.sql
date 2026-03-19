-- Chat profiles: per-chat glossaries, member names, tone & context
-- Built automatically by analytics chat_context_builder flow

CREATE TABLE IF NOT EXISTS public.chat_profiles (
    id              bigserial PRIMARY KEY,
    chat_pair_id    bigint NOT NULL REFERENCES public.chat_pairs(id) ON DELETE CASCADE UNIQUE,
    profile_data    jsonb NOT NULL DEFAULT '{}',
    version         integer NOT NULL DEFAULT 1,
    tokens_used     integer DEFAULT 0,
    estimated_cost  numeric(10,6) DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.chat_profile_history (
    id              bigserial PRIMARY KEY,
    chat_pair_id    bigint NOT NULL REFERENCES public.chat_pairs(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    profile_data    jsonb NOT NULL,
    change_summary  text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_profiles_pair ON public.chat_profiles(chat_pair_id);
CREATE INDEX IF NOT EXISTS idx_chat_profile_history_pair ON public.chat_profile_history(chat_pair_id);
