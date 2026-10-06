-- 021: our own ledger of LLM calls, replacing LangSmith.
--
-- LangSmith traced every processor call to a third party (full message texts of parents'
-- chats included) and was the only place translation cost was known; GET /api/costs read
-- it from there. The processor now calls OpenAI directly (src/llm.py) and writes one row
-- per call here instead.
--
--   purpose   translate | translate_retry | direct_translate | image | document |
--             voice_translate | transcribe
--   tag       translate: the A/B version string (e.g. v2.10@gpt-6-luna), else NULL
--   cost_usd  from config.MODEL_PRICES / TRANSCRIBE_PRICES at call time

CREATE TABLE IF NOT EXISTS llm_usage (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    purpose     text NOT NULL,
    model       text NOT NULL,
    tag         text,
    tokens_in   integer NOT NULL DEFAULT 0,
    tokens_out  integer NOT NULL DEFAULT 0,
    cost_usd    numeric(12, 6) NOT NULL DEFAULT 0,
    ms          integer
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage (created_at);
