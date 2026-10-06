-- 018: tell the Jev classifier's scores apart from the LLM judge's.
--
-- translation_quality can now score translations with TypeSafe's Jev (see
-- analytics/flows/jev_eval.py). In shadow mode Jev scores every translation alongside the
-- usual LLM sample so the two can be compared; those rows must not leak into the averages
-- the weekly report and the dashboard read, hence `shadow`.
--
--   evaluator         'llm' | 'jev'
--   shadow            true = comparison data only, excluded from reports
--   quality_expected  Jev's probability-weighted quality on the 1-5 scale (NULL for llm)
--   confidence        Jev's confidence in that quality score (NULL for llm)

ALTER TABLE translation_evaluations
    ADD COLUMN IF NOT EXISTS evaluator text NOT NULL DEFAULT 'llm',
    ADD COLUMN IF NOT EXISTS shadow boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS quality_expected real,
    ADD COLUMN IF NOT EXISTS confidence real;
