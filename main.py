import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from monitor.bot.max_adapter import MaxAdapter
from monitor.config import settings
from monitor.db import init_db
from monitor.flows import KorsikFlows


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    adapter = MaxAdapter()
    flows = KorsikFlows(adapter)
    adapter.bind(
        start=flows.start,
        message=flows.message,
        callback=flows.callback,
        bot_started=flows.bot_started,
    )

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.TIMEZONE))
    scheduler.add_job(
        flows.timeline.run_due,
        trigger="interval",
        seconds=15,
        id="onboarding-timeline",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        flows.run_survey_reminders,
        trigger="interval",
        seconds=60,
        id="survey-reminders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        flows.run_hr_sla_reminders,
        trigger="interval",
        seconds=60,
        id="hr-sla-reminders",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        flows.send_weekly_digest,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        id="weekly-hr-digest",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Корсик запущен: long polling, timezone=%s, TIME_SCALE=%s",
        settings.TIMEZONE,
        settings.TIME_SCALE,
    )
    try:
        await adapter.run()
    finally:
        scheduler.shutdown(wait=False)
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
