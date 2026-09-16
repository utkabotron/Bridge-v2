-- 013: media_analysis.message_event_id — добавить on delete cascade.
-- Без каскада ежедневный флоу daily-cleanup падал на первой же задаче
-- (delete from message_events older than 90 days → ForeignKeyViolation),
-- и из-за обрыва флоу не чистились и остальные таблицы. Анализ медиа живёт
-- ровно столько, сколько живёт породившее его событие, поэтому cascade здесь
-- корректнее, чем блокировка удаления.

alter table public.media_analysis
  drop constraint media_analysis_message_event_id_fkey;

alter table public.media_analysis
  add constraint media_analysis_message_event_id_fkey
  foreign key (message_event_id)
  references public.message_events(id)
  on delete cascade;
