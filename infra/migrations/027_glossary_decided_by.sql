-- 027: who settled a glossary entry — 'auto' (resolver agreed with the chats) or 'admin'
-- (✅ / ✏️ / ❌ under the morning digest, or /api/glossary). Automation must never overrule
-- an admin decision; the digest counts what was accepted automatically.

ALTER TABLE glossary ADD COLUMN IF NOT EXISTS decided_by text
    CHECK (decided_by IN ('auto', 'admin'));

UPDATE glossary SET decided_by = 'auto'  WHERE status IN ('verified', 'rejected') AND decided_by IS NULL;
UPDATE glossary SET decided_by = 'admin' WHERE status = 'locked' AND decided_by IS NULL;
