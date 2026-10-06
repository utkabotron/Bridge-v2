-- 019: enough provenance on translations and their evaluations to measure a change.
--
-- Nothing recorded which prompt produced a translation, whether it came from the cache,
-- or whether the LLM failed or echoed the source. The evaluator's rows carried no pair,
-- language or message type, and bot DM translations (the other direction) were mixed into
-- the same averages as the bridge. So v2.6 → v2.9 could not be compared, the worst chat
-- could not be named, and 16% of the "quality" sample was admin fallback and delivery
-- failures scored as translation defects.
--
-- message_events:
--   prompt_version          PROMPT_VERSION that produced translated_text (NULL = not translated)
--   cache_hit               translation served from Redis
--   translation_passthrough model echoed the source and the retry did not fix it
--   translation_failed      LLM unreachable, original delivered untranslated
--   translation_error       why
--
-- translation_evaluations:
--   source          'bridge' (paired chat) | 'direct' (bot DM) | 'fallback' (unpaired admin chat)
--   chat_pair_id, target_language, message_type  denormalised: evaluations outlive the
--                   events (90-day retention cascades differently) and reports group by them
--   prompt_version  copied from the event at evaluation time

ALTER TABLE message_events
    ADD COLUMN IF NOT EXISTS prompt_version text,
    ADD COLUMN IF NOT EXISTS cache_hit boolean,
    ADD COLUMN IF NOT EXISTS translation_passthrough boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS translation_failed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS translation_error text;

ALTER TABLE translation_evaluations
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'bridge',
    ADD COLUMN IF NOT EXISTS chat_pair_id bigint,
    ADD COLUMN IF NOT EXISTS target_language text,
    ADD COLUMN IF NOT EXISTS message_type text,
    ADD COLUMN IF NOT EXISTS prompt_version text;

CREATE INDEX IF NOT EXISTS idx_translation_evaluations_source_pair
    ON translation_evaluations(source, chat_pair_id);

-- Back-fill what can be recovered, so the breakdowns start with history.
UPDATE translation_evaluations te
SET chat_pair_id = me.chat_pair_id,
    message_type = me.message_type,
    target_language = coalesce(cp.target_language, u.target_language),
    source = CASE WHEN me.chat_pair_id IS NULL THEN 'fallback' ELSE 'bridge' END
FROM message_events me
LEFT JOIN chat_pairs cp ON cp.id = me.chat_pair_id
LEFT JOIN users u ON u.id = cp.user_id
WHERE me.id = te.message_event_id
  AND te.chat_pair_id IS NULL;

UPDATE translation_evaluations
SET source = 'direct'
WHERE message_event_id IS NULL AND source = 'bridge';
