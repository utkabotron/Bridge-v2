-- 016: let chat_pairs.target_language mean "inherit the account setting".
--
-- The column already existed on the live database but in no migration, as
-- `not null default 'Russian'`. Two consequences:
--   * every pair the bot created was pinned to Russian at insert time, whatever the
--     owner's account language was, and changing the account language moved nothing;
--   * "follow my account" could not be expressed at all, because NULL was forbidden.
--
-- Making it nullable gives NULL that meaning, which is what
-- coalesce(cp.target_language, u.target_language) in the queries expects.

alter table public.chat_pairs
  alter column target_language drop default,
  alter column target_language drop not null;

-- Rows that merely repeat the owner's language were not deliberate overrides — they are
-- the old default. Clear them so those bridges follow the account from now on. A pair
-- whose language genuinely differs is left alone.
update public.chat_pairs cp
set target_language = null
from public.users u
where u.id = cp.user_id
  and cp.target_language is not distinct from u.target_language;
