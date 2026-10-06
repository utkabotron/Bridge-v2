-- 025: one glossary of names for the whole service (docs/glossary-plan.md).
--
-- Chat profiles each guessed the vowels of a Hebrew name on their own — one school came
-- out four ways across seven chats, one boy's name both Ори and Ури. A name now has one
-- rendering per target language, read from a source (the official Latin spelling on a
-- website or map, or the established transliteration of a first name):
--
--   candidate  collected from chat profiles, not resolved yet          — not used
--   proposed   resolver's answer, waits for the admin in the digest     — not used
--   verified   resolver agreed with the chats, or the admin approved    — used
--   locked     set by hand; nothing automatic touches it               — used
--   rejected   not a name, or a wrong reading; never proposed again     — not used
--
-- People's names are stored word by word (דנה, דרחי), never with who they are: relations
-- ("child of …") stay in the chat's own profile and never cross between users.
--
-- glossary_override pins a different rendering for one chat only (two different Ofeks).
-- The processor keeps both tables in memory and reloads them when count/max(updated_at)
-- change, so every write must touch updated_at.

CREATE TABLE IF NOT EXISTS glossary (
    id               serial PRIMARY KEY,
    source           text NOT NULL,                 -- as written in messages, normalised
    target_language  text NOT NULL DEFAULT 'Russian',
    translation      text,                          -- NULL while a candidate
    kind             text NOT NULL DEFAULT 'other'
                     CHECK (kind IN ('person', 'place', 'org', 'other')),
    note             text,
    status           text NOT NULL DEFAULT 'candidate'
                     CHECK (status IN ('candidate', 'proposed', 'verified', 'locked', 'rejected')),
    evidence         text,                          -- official Latin spelling and/or URL
    confidence       real,
    chat_renderings  jsonb NOT NULL DEFAULT '{}',   -- {rendering: chats that used it}
    chats_seen       integer NOT NULL DEFAULT 0,
    resolver_model   text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    UNIQUE (source, target_language)
);

CREATE INDEX IF NOT EXISTS glossary_status_idx ON glossary (status);

CREATE TABLE IF NOT EXISTS glossary_override (
    chat_pair_id     integer NOT NULL REFERENCES chat_pairs(id) ON DELETE CASCADE,
    source           text NOT NULL,
    target_language  text NOT NULL DEFAULT 'Russian',
    translation      text NOT NULL,
    note             text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_pair_id, source, target_language)
);

-- 024's hand-pinned entries carry over as locked.
INSERT INTO glossary (source, target_language, translation, kind, note, status, decided_at)
SELECT source, target_language, translation, 'other', note, 'locked', updated_at
FROM glossary_global
ON CONFLICT (source, target_language) DO NOTHING;

UPDATE glossary SET kind = 'org' WHERE source = 'גבעולים' AND kind = 'other';

DROP TABLE IF EXISTS glossary_global;
