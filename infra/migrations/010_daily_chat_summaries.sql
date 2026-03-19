-- Daily chat summaries: per-chat daily digests sent to TG groups
-- Built automatically by analytics daily_chat_summary flow

CREATE TABLE IF NOT EXISTS public.daily_chat_summaries (
    id              bigserial PRIMARY KEY,
    chat_pair_id    bigint NOT NULL REFERENCES public.chat_pairs(id) ON DELETE CASCADE,
    summary_date    date NOT NULL,
    message_count   integer NOT NULL DEFAULT 0,
    unique_senders  integer NOT NULL DEFAULT 0,
    summary_text    text,
    plans_extracted jsonb DEFAULT '[]',
    optimal_send_hour integer,
    sent            boolean NOT NULL DEFAULT false,
    sent_at         timestamptz,
    tg_message_id   bigint,
    tokens_used     integer DEFAULT 0,
    estimated_cost  numeric(10,6) DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chat_pair_id, summary_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON public.daily_chat_summaries(summary_date);
CREATE INDEX IF NOT EXISTS idx_daily_summaries_sent ON public.daily_chat_summaries(sent, summary_date);

CREATE TABLE IF NOT EXISTS public.chat_summary_schedule (
    chat_pair_id    bigint PRIMARY KEY REFERENCES public.chat_pairs(id) ON DELETE CASCADE,
    optimal_hour    integer NOT NULL DEFAULT 22 CHECK (optimal_hour BETWEEN 0 AND 23),
    optimal_minute  integer NOT NULL DEFAULT 0 CHECK (optimal_minute IN (0, 30)),
    mean_hour       numeric(5,2),
    std_hour        numeric(5,2),
    sample_size     integer DEFAULT 0,
    computed_at     timestamptz NOT NULL DEFAULT now()
);
