-- 028: bad translations put a service-glossary entry "под вопросом" instead of removing it.
--
-- Chat glossaries lose an entry after three evaluator flags (flows/glossary.py), which is
-- fine for one chat's guess. A service entry was checked by a source or by hand, so the
-- nightly feedback only counts flags (with the last examples); the morning digest lists
-- entries at the threshold and they are reviewed by hand. Locked entries are not flagged.

ALTER TABLE glossary ADD COLUMN IF NOT EXISTS flags integer NOT NULL DEFAULT 0;
ALTER TABLE glossary ADD COLUMN IF NOT EXISTS flag_examples jsonb NOT NULL DEFAULT '[]';
