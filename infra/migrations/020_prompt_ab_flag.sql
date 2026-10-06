-- 020: a switch for the translation prompt A/B test.
--
-- Prompt changes used to go straight to every chat with nothing to compare against, so
-- three months of "applied" suggestions could not be shown to have helped (v2.6 → v2.9:
-- 4.59 → 4.54). With prompt_ab_enabled on, odd-numbered chat pairs are translated with
-- variant B (processor/src/pipeline/prompts.py: SYSTEM_TRANSLATE_B) and every message
-- and evaluation records its prompt_version, so the nightly breakdown compares the two.
-- Off by default; toggle from the dashboard (PATCH /api/flags/prompt_ab_enabled).

INSERT INTO feature_flags (name, enabled) VALUES ('prompt_ab_enabled', false)
ON CONFLICT (name) DO NOTHING;
