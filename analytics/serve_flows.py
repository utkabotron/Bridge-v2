"""Register all flows with local Prefect server and serve them on schedule."""
from prefect import serve

from flows.nightly_problems import nightly_problems
from flows.translation_quality import translation_quality
from flows.cleanup import daily_cleanup
from flows.health_check import wa_health_check
from flows.weekly_report import weekly_report
if __name__ == "__main__":
    serve(
        nightly_problems.to_deployment(
            name="nightly-problems",
            cron="0 4 * * *",
        ),
        translation_quality.to_deployment(
            name="translation-quality",
            cron="30 4 * * *",
        ),
        daily_cleanup.to_deployment(
            name="daily-cleanup",
            cron="0 3 * * *",
        ),
        wa_health_check.to_deployment(
            name="wa-health-check",
            cron="*/15 * * * *",
        ),
        weekly_report.to_deployment(
            name="weekly-report",
            cron="0 5 * * 1",
        ),
    )
