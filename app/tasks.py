"""Celery tasks — the periodic reminder work runs here, not in FastAPI."""
import asyncio
import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run(unit_of_work):
    """Run one async unit of work against a fresh engine in a fresh event loop.

    A new engine per invocation avoids reusing connections bound to a previous,
    now-closed loop ("Future attached to a different loop").
    """

    async def _main():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.config import settings

        engine = create_async_engine(settings.database_url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as db:
                return await unit_of_work(db)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


@celery_app.task(name="app.tasks.poll_reminders", bind=True, max_retries=3)
def poll_reminders(self):
    """Send reminder events whose trigger time has passed, and retire finished courses."""
    from app.scheduler import run_poll_cycle

    try:
        return _run(run_poll_cycle)
    except Exception as exc:
        logger.exception("poll_reminders task failed")
        raise self.retry(exc=exc, countdown=30)


@celery_app.task(name="app.tasks.top_up_reminders", bind=True, max_retries=3)
def top_up_reminders(self):
    """Daily upkeep: extend the dose window and reproject refill alerts.

    Dose events are otherwise only created when a user adds or changes a
    medication, so without this a user who stops texting stops getting
    reminders. Refill projections likewise go stale as stock is consumed.
    """
    from app.services.reminder_service import run_daily_maintenance

    try:
        result = _run(run_daily_maintenance)
        logger.info("Daily reminder maintenance: %s", result)
        return result
    except Exception as exc:
        logger.exception("top_up_reminders task failed")
        raise self.retry(exc=exc, countdown=300)
