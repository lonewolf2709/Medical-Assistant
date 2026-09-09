"""Celery application — replaces the asyncio background scheduler."""
import logging

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

celery_app = Celery(
    "medibuddy",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],  # explicitly import tasks module so worker registers them
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Beat schedule — run poll_reminders every 60 seconds
    beat_schedule={
        "poll-reminders": {
            "task": "app.tasks.poll_reminders",
            "schedule": settings.scheduler_interval_seconds,
        },
        # Keeps the precomputed reminder window rolling forward. Without it,
        # reminders stop once the window created at add-time runs out.
        "top-up-reminders": {
            "task": "app.tasks.top_up_reminders",
            "schedule": crontab(hour=1, minute=0),
        },
    },
)
