-- 026: names that are also everyday words stay in the chats that use them.
--
-- With resolver-verified names in every prompt, "אני עמוס היום" (I'm busy today) came out
-- "Я сегодня Амос", and "יש לנו אופק חדש" (a new horizon) "новый Офек". An entry whose
-- spelling is also an ordinary word (also_word) is applied only in the chats it was seen in
-- (chat_pairs, filled by the resolver's import) — what the chat's own glossary did before;
-- an unambiguous one (Рамат-Ган, PayBox) applies everywhere. NULL = not classified yet,
-- treated as ambiguous. Locked entries are the admin's call and apply everywhere.

ALTER TABLE glossary ADD COLUMN IF NOT EXISTS also_word boolean;
ALTER TABLE glossary ADD COLUMN IF NOT EXISTS chat_pairs integer[] NOT NULL DEFAULT '{}';
