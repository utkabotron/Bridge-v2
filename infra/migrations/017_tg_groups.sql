-- 017: remember Telegram groups the bot is in, per admin who may link them.
--
-- The Mini App's group picker read a Redis hash (bot:user_groups:{id}) written only by the
-- my_chat_member event, keyed by whoever added the bot, with a one-hour TTL. Three things
-- followed: a group the bot was already in never appeared (no event was ever emitted for
-- it), a group added by a different admin landed under that admin's key, and after an hour
-- of no new additions the list simply emptied. The picker was therefore blank for most
-- users most of the time.
--
-- Postgres instead of Redis because this is durable state, not a cache: the list must
-- survive restarts and must not expire.

create table if not exists public.tg_groups (
  tg_chat_id  bigint      not null,
  -- One row per (group, admin): every admin of the group may link it, and the picker
  -- shows a user exactly the groups they are entitled to link.
  tg_user_id  bigint      not null,
  title       text        not null default '',
  role        text        not null,          -- creator | administrator
  updated_at  timestamptz not null default now(),
  primary key (tg_chat_id, tg_user_id)
);

create index if not exists idx_tg_groups_user on public.tg_groups (tg_user_id, updated_at desc);
