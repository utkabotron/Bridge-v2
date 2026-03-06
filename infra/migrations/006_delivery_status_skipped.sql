-- 006: Add 'skipped' delivery_status for unmapped messages (no chat pair).
-- Previously these were marked as 'failed', inflating failure metrics.

ALTER TABLE public.message_events
    DROP CONSTRAINT IF EXISTS message_events_delivery_status_check;

ALTER TABLE public.message_events
    ADD CONSTRAINT message_events_delivery_status_check
    CHECK (delivery_status IN ('pending', 'delivered', 'failed', 'skipped'));

-- Reclassify existing no_chat_pair records from 'failed' to 'skipped'
UPDATE public.message_events
    SET delivery_status = 'skipped'
    WHERE delivery_status = 'failed'
      AND error_message = 'no_chat_pair';
