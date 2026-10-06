-- 024: a service-wide glossary of names, curated by hand.
--
-- Chat glossaries are built per chat by an LLM reading Hebrew originals, so each chat
-- guesses the vowels of a name on its own: one school (גבעולים) came out as Гивъолим,
-- Гевалим, Геваулим and Гвуллим across seven chats of three different users. Entries here
-- win over a chat's own entry for the same name and are never proposed, flagged or
-- removed by the nightly builder. Edited through /api/glossary (processor), which drops
-- the processor's Redis copy (glossary_global:{lang}).

CREATE TABLE IF NOT EXISTS glossary_global (
    id              serial PRIMARY KEY,
    source          text NOT NULL,                     -- Hebrew name as it appears in messages
    target_language text NOT NULL DEFAULT 'Russian',
    translation     text NOT NULL,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, target_language)
);

INSERT INTO glossary_global (source, target_language, translation, note)
VALUES ('גבעולים', 'Russian', 'Гиволим', 'название школы в Рамат-Гане')
ON CONFLICT (source, target_language) DO NOTHING;
