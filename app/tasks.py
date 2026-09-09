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
    """Send reminder events whose trigger time has passed."""
    from app.scheduler import _process_due_events

    try:
        return _run(_process_due_events)
    except Exception as exc:
        logger.exception("poll_reminders task failed")
        raise self.retry(exc=exc, countdown=30)


@celery_app.task(name="app.tasks.top_up_reminders", bind=True, max_retries=3)
def top_up_reminders(self):
    """Extend every active medication's dose events to cover the coming days.

    Events are otherwise only created when a user adds or changes a medication,
    so without this a user who stops texting stops getting reminders.
    """
    from app.services.reminder_service import top_up_dose_events

    try:
        topped_up = _run(top_up_dose_events)
        logger.info("Topped up dose events for %d medications", topped_up)
        return topped_up
    except Exception as exc:
        logger.exception("top_up_reminders task failed")
        raise self.retry(exc=exc, countdown=300)
