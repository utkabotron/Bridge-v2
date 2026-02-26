-- Bridge v2 — Initial Schema
-- 4 tables: users, chat_pairs, message_events, onboarding_sessions
-- PostgreSQL 16+

-- ── users ─────────────────────────────────────────────────
create table if not exists public.users (
  id              bigserial primary key,
  tg_user_id      bigint not null unique,
  tg_username     text,
  is_admin        boolean not null default false,
  is_active       boolean not null default true,
  wa_session_id   text,
  wa_connected    boolean not null default false,
  target_language text not null default 'Hebrew',
  created_at      timestamptz not null default now()
);

create index if not exists idx_users_tg_user_id on public.users (tg_user_id);

-- ── chat_pairs ────────────────────────────────────────────
create table if not exists public.chat_pairs (
  id            bigserial primary key,
  user_id       bigint not null references public.users (id) on delete cascade,
  wa_chat_id    text not null,
  wa_chat_name  text,
  tg_chat_id    bigint not null,
  tg_chat_title text,
  status        text not null default 'active'
                  check (status in ('active', 'paused')),
  created_at    timestamptz not null default now(),
  unique (user_id, wa_chat_id, tg_chat_id)
);

create index if not exists idx_chat_pairs_user_id on public.chat_pairs (user_id);
create index if not exists idx_chat_pairs_wa_chat on public.chat_pairs (wa_chat_id, status);

-- ── message_events ────────────────────────────────────────
-- Operational log and BigQuery export source.
create table if not exists public.message_events (
  id              bigserial primary key,
  wa_message_id   text not null unique,
  chat_pair_id    bigint references public.chat_pairs (id) on delete set null,
  sender_name     text,
  original_text   text,
  translated_text text,
  message_type    text not null default 'text',
  media_s3_key    text,
  translation_ms  int,
  delivery_status text not null default 'pending'
                    check (delivery_status in ('pending', 'delivered', 'failed')),
  error_message   text,
  created_at      timestamptz not null default now()
);

create index if not exists idx_msg_events_chat_pair on public.message_events (chat_pair_id);
create index if not exists idx_msg_events_status    on public.message_events (delivery_status);
create index if not exists idx_msg_events_created   on public.message_events (created_at);

-- ── onboarding_sessions ───────────────────────────────────
create table if not exists public.onboarding_sessions (
  user_id    bigint primary key references public.users (id) on delete cascade,
  state      text not null default 'idle'
               check (state in ('idle', 'qr_pending', 'wa_connected', 'linking', 'done')),
  started_at timestamptz not null default now(),
  done_at    timestamptz
);
