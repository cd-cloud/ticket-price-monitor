import logging
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger


LOGGER = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self, timezone: str, interval_minutes: int) -> None:
        self.scheduler = BlockingScheduler(timezone=timezone)
        self.interval_minutes = interval_minutes

    def add_price_job(self, job: Callable[[], None], interval_minutes: int | None = None) -> None:
        minutes = interval_minutes or self.interval_minutes
        self.scheduler.add_job(
            job,
            trigger=IntervalTrigger(minutes=minutes),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            id="flight-price-check",
            replace_existing=True,
        )
        LOGGER.info("scheduled price check every %s minute(s)", minutes)

    def run_forever(self) -> None:
        LOGGER.info("scheduler started")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            LOGGER.info("scheduler stopped")
        finally:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
