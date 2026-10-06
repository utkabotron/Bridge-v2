-- 023: Drop write-only column users.wa_session_id.
--
-- This column was written at bot/src/db.py:set_wa_connected() with str(user_id)
-- but never read anywhere in the codebase (grep confirmed: no readers in processor/,
-- bot/, or wa-service/). The write was redundant cover for wa-service's own write,
-- but since no code ever queried it, the column wasted space. Simplify by removing it
-- and the unnecessary parameter from set_wa_connected().

ALTER TABLE users DROP COLUMN IF EXISTS wa_session_id;
