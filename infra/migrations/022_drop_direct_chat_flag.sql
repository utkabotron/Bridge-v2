-- 022: Drop unused feature flag direct_chat_enabled.
--
-- This flag was seeded in 011_feature_flags.sql but never referenced anywhere in the
-- codebase. No code calls is_enabled("direct_chat_enabled"), so it has zero effect on
-- pipeline behavior. Removing it simplifies feature flag registry.

DELETE FROM feature_flags WHERE name = 'direct_chat_enabled';
