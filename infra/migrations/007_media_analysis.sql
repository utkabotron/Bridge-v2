-- 007: Add tg_message_id to message_events + media_analysis table
-- Required for inline "Analyze" button on media messages

ALTER TABLE message_events ADD COLUMN IF NOT EXISTS tg_message_id bigint;

CREATE TABLE IF NOT EXISTS media_analysis (
    id serial PRIMARY KEY,
    message_event_id integer NOT NULL REFERENCES message_events(id),
    analysis_type varchar(20) NOT NULL,  -- image | audio | document
    result_text text NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'completed',  -- completed | failed
    processing_ms integer,
    requested_by bigint,  -- tg_user_id who pressed the button
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_media_analysis_event ON media_analysis(message_event_id);
