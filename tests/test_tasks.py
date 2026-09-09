"""Celery wiring for the periodic reminder work."""


def test_beat_schedule_polls_for_due_reminders():
    from app.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["poll-reminders"]
    assert entry["task"] == "app.tasks.poll_reminders"


def test_beat_schedule_tops_up_dose_events():
    """Without a periodic top-up, reminders stop when the precomputed window runs out."""
    from app.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    tasks = {entry["task"] for entry in schedule.values()}
    assert "app.tasks.top_up_reminders" in tasks, f"scheduled tasks: {tasks}"


def test_both_periodic_tasks_are_registered_on_the_worker():
    import app.tasks  # noqa: F401  (registers the tasks)
    from app.celery_app import celery_app

    for name in ("app.tasks.poll_reminders", "app.tasks.top_up_reminders"):
        assert name in celery_app.tasks, f"{name} is not registered"
