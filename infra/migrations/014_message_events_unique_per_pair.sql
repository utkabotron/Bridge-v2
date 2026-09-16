-- 014: ключ дедупа message_events — на (сообщение, пара), а не на сообщение.
-- Одно сообщение WhatsApp-группы теперь доставляется во ВСЕ активные пары этого
-- чата (fan-out), то есть порождает несколько строк с одним wa_message_id.
-- Старый unique(wa_message_id) схлопывал их в одну и терял доставки.
--
-- nulls not distinct (Postgres 15+) сохраняет дедуп и для строк без пары
-- (delivery_status = 'skipped' и fallback-to-admins, где chat_pair_id is null):
-- при обычном unique два NULL считались бы различными и дубли проходили бы.

alter table public.message_events
  drop constraint message_events_wa_message_id_key;

alter table public.message_events
  add constraint message_events_wa_message_id_chat_pair_id_key
  unique nulls not distinct (wa_message_id, chat_pair_id);
