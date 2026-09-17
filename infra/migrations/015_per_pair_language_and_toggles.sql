-- 015: per-pair target language, a summary on/off switch, and the voice transcript flag.
--
-- target_language lived only on users, so one person bridging a Hebrew school group and a
-- Spanish work chat had to pick one language for both. The column is nullable and the
-- queries read coalesce(cp.target_language, u.target_language), so every existing pair
-- keeps behaving exactly as before until its owner changes it.

alter table public.chat_pairs
  add column if not exists target_language text;

comment on column public.chat_pairs.target_language is
  'Overrides users.target_language for this bridge. NULL = inherit the user''s setting.';

-- Daily summaries could not be turned off: the schedule row is created by the flow and
-- there was no way for a user to say "not for this chat".
alter table public.chat_summary_schedule
  add column if not exists enabled boolean not null default true;

insert into public.feature_flags (name, enabled) values
  ('voice_transcribe_enabled', true)
on conflict (name) do nothing;
