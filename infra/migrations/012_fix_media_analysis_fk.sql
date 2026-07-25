-- 012: Widen media_analysis.message_event_id to bigint.
-- message_events.id is bigserial (bigint); the FK column was integer (007), so inserts
-- would start failing once message_events.id crosses 2^31. Align the types now.

ALTER TABLE media_analysis
  ALTER COLUMN message_event_id TYPE bigint;
