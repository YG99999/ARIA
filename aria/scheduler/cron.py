"""APScheduler wrapper — BackgroundScheduler (not AsyncIOScheduler).

APScheduler 3.x SQLAlchemyJobStore makes synchronous DB calls that block the
async event loop. Use BackgroundScheduler (runs in its own thread) and communicate
via a thread-safe asyncio.Queue.

APScheduler callbacks MUST be sync functions. Never make them async or call async
functions directly — APScheduler runs callbacks in its own thread, not the event loop.
All job callbacks use call_soon_threadsafe() to enqueue tasks.
"""

import asyncio
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Thread-safe queue: scheduler thread → event loop
_job_queue: asyncio.Queue | None = None
_scheduler: Any = None
_loop: asyncio.AbstractEventLoop | None = None


def init_scheduler(loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
    """Initialize the BackgroundScheduler and return the job queue."""
    global _scheduler, _job_queue, _loop
    _loop = loop
    _job_queue = asyncio.Queue()

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from core.config import settings

    db_url = f"sqlite:///{settings.state_db}"
    _scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=db_url, tablename="apscheduler_jobs")},
        job_defaults={"max_instances": 1, "misfire_grace_time": 60},
    )
    _scheduler.start()
    logger.info("APScheduler started")
    return _job_queue


def _make_job_callback(description: str):
    """Return a sync callback that enqueues the task description thread-safely."""
    def callback():
        if _loop and _job_queue:
            _loop.call_soon_threadsafe(_job_queue.put_nowait, description)
        else:
            logger.error("Scheduler callback: loop or queue not initialized")
    return callback


class CronScheduler:

    @staticmethod
    async def add_job(description: str, trigger_text: str) -> str:
        """Parse natural language trigger and add to APScheduler."""
        from core.database import state_db

        trigger_type, trigger_kwargs = _parse_trigger(trigger_text)
        job_id = str(uuid.uuid4())[:8]

        if _scheduler:
            if trigger_type == "cron":
                from apscheduler.triggers.cron import CronTrigger
                trigger = CronTrigger(**trigger_kwargs)
            else:
                from apscheduler.triggers.interval import IntervalTrigger
                trigger = IntervalTrigger(**trigger_kwargs)

            callback = _make_job_callback(description)
            _scheduler.add_job(
                callback,
                trigger=trigger,
                id=job_id,
                name=description[:50],
                replace_existing=True,
            )

        # Persist in schedule table
        import json
        await state_db.execute(
            "INSERT OR REPLACE INTO schedule(id, description, trigger, trigger_args) VALUES (?, ?, ?, ?)",
            (job_id, description, trigger_text, json.dumps(trigger_kwargs)),
        )
        await state_db.commit()
        return job_id

    @staticmethod
    async def remove_job(job_id: str) -> None:
        from core.database import state_db

        if _scheduler:
            try:
                _scheduler.remove_job(job_id)
            except Exception:
                pass
        await state_db.execute("DELETE FROM schedule WHERE id=?", (job_id,))
        await state_db.commit()

    @staticmethod
    def add_system_jobs() -> None:
        """Register built-in system maintenance jobs."""
        if not _scheduler:
            return

        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        # Health snapshot every 5 minutes
        _scheduler.add_job(
            _make_job_callback("__system:health_snapshot"),
            IntervalTrigger(minutes=5),
            id="sys_health", replace_existing=True,
        )

        # Journal compaction daily at 3am
        _scheduler.add_job(
            _make_job_callback("__system:journal_compact"),
            CronTrigger(hour=3, minute=0),
            id="sys_journal_compact", replace_existing=True,
        )

        # Browser health check daily at 4am
        _scheduler.add_job(
            _make_job_callback("__system:browser_health"),
            CronTrigger(hour=4, minute=0),
            id="sys_browser_health", replace_existing=True,
        )

        # Chromium process cleanup daily at 3am
        _scheduler.add_job(
            _make_job_callback("__system:chromium_cleanup"),
            CronTrigger(hour=3, minute=0),
            id="sys_chromium_cleanup", replace_existing=True,
        )

        # Dream cycle daily at 3:30am
        _scheduler.add_job(
            _make_job_callback("__system:dream_cycle"),
            CronTrigger(hour=3, minute=30),
            id="sys_dream_cycle", replace_existing=True,
        )

        # Morning briefing — OFF by default.
        # ARIA enables this after a week of use (or on user request via /schedule).
        # Stored in facts as: key="morning_briefing_hour", value="8" (hour, 24h).
        # Enabled when fact "morning_briefing_enabled" == "1".
        # We register a daily sentinel job — the handler checks the enabled flag.
        _scheduler.add_job(
            _make_job_callback("__system:morning_briefing"),
            CronTrigger(hour=8, minute=0),  # Fires at 8am; handler checks if enabled
            id="sys_morning_briefing", replace_existing=True,
        )

        logger.info("System jobs registered")


def _parse_trigger(text: str) -> tuple[str, dict]:
    """Parse natural language trigger into APScheduler trigger args.

    Returns ('cron', {hour, minute}) or ('interval', {minutes/hours}).
    """
    text = text.lower().strip()

    # "daily at Xam/pm"
    m = re.search(r"daily at (\d+)(?::(\d+))?\s*(am|pm)?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = m.group(3)
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        return "cron", {"hour": hour, "minute": minute}

    # "every X minutes"
    m = re.search(r"every (\d+) minutes?", text)
    if m:
        return "interval", {"minutes": int(m.group(1))}

    # "every X hours"
    m = re.search(r"every (\d+) hours?", text)
    if m:
        return "interval", {"hours": int(m.group(1))}

    # "weekly on Monday at Xam"
    days = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}
    for day_name, day_num in days.items():
        if day_name in text:
            m = re.search(r"at (\d+)(?::(\d+))?\s*(am|pm)?", text)
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2) or 0)
                meridiem = m.group(3)
                if meridiem == "pm" and hour != 12:
                    hour += 12
                return "cron", {"day_of_week": day_num, "hour": hour, "minute": minute}

    # Default: daily at 9am
    logger.warning("Could not parse trigger %r — defaulting to daily 9am", text)
    return "cron", {"hour": 9, "minute": 0}
