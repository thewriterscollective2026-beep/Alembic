"""Scheduling.

Two kinds of jobs:
  1. Per-user RUN job — fires on the user's frequency at their chosen hour (UTC)
     and calls start_run (scrape -> summary -> email the user).
  2. One global POLL job — every 10 minutes, reads replies for every run awaiting
     review and advances it (summary approval -> write draft -> final send)."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .database import SessionLocal
from .models import User
from .pipeline import start_run, poll_all_pending

_scheduler: BackgroundScheduler | None = None

# Each maps to CronTrigger kwargs (day-of-week or day-of-month based)
FREQUENCY_CRON = {
    "daily": {"day_of_week": "*"},
    "twice_week": {"day_of_week": "mon,thu"},
    "thrice_week": {"day_of_week": "mon,wed,fri"},
    "weekly": {"day_of_week": "mon"},
    "twice_monthly": {"day": "1,15"},
}

FREQUENCY_LABELS = {
    "daily": "Every day (reads the last 1 day)",
    "thrice_week": "3× a week — Mon/Wed/Fri (reads the last 2 days)",
    "twice_week": "2× a week — Mon/Thu (reads the last 3 days)",
    "weekly": "Once a week — Mon (reads the last 7 days)",
    "twice_monthly": "Twice a month — 1st & 15th (reads the last 15 days)",
}

POLL_MINUTES = 3  # how often to check the inbox for approval replies


def start():
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.start()
        # Global reply poller
        _scheduler.add_job(
            poll_all_pending, trigger=IntervalTrigger(minutes=POLL_MINUTES),
            id="reply_poller", replace_existing=True, misfire_grace_time=300,
        )
        _reload_all()


def _job_id(user_id: int) -> str:
    return f"run_user_{user_id}"


def _reload_all():
    db = SessionLocal()
    try:
        for user in db.query(User).filter_by(schedule_active=True).all():
            schedule_user(user.id, user.frequency, user.run_hour)
    finally:
        db.close()


def schedule_user(user_id: int, frequency: str, run_hour: int = 7):
    if _scheduler is None:
        start()
    unschedule_user(user_id)
    cron = FREQUENCY_CRON.get(frequency)
    if not cron:
        return
    _scheduler.add_job(
        start_run,
        trigger=CronTrigger(hour=int(run_hour), minute=0, **cron),
        id=_job_id(user_id), args=[user_id],
        replace_existing=True, misfire_grace_time=3600,
    )


def unschedule_user(user_id: int):
    if _scheduler is None:
        return
    job = _scheduler.get_job(_job_id(user_id))
    if job:
        job.remove()
