"""ARIA entry point.

Usage:
    python main.py           # normal start
    python main.py --setup   # run onboarding wizard
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

# Ensure aria/ package is importable when running `python main.py` from aria/
sys.path.insert(0, str(Path(__file__).parent))


def _configure_logging() -> None:
    log_dir = Path(__file__).parent / "data"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "aria.log"

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


async def _run() -> None:
    from core.config import settings
    from core.database import init_all_databases
    from core.tools import discover_tools

    settings.load()
    logger = logging.getLogger(__name__)
    logger.info("ARIA starting up")

    await init_all_databases(settings.state_db, settings.memory_db, settings.journal_db)
    logger.info("Databases ready")

    discover_tools()
    logger.info("Tools discovered")

    # Phase 1+ modules — imported lazily so Phase 0 tests work without them
    from core.orchestrator import Orchestrator  # noqa: PLC0415
    from tg.bot import TelegramBot  # noqa: PLC0415
    from tg.notify import sender_loop  # noqa: PLC0415
    from scheduler.cron import init_scheduler, CronScheduler  # noqa: PLC0415
    from system.webhook import run_webhook_server  # noqa: PLC0415

    bot = TelegramBot()
    orchestrator = Orchestrator(bot)

    shutdown_event = asyncio.Event()
    _shutting_down = False

    def _handle_signal(sig: signal.Signals) -> None:  # type: ignore[name-defined]
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        logger.info("Received signal %s — initiating graceful shutdown", sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows does not support add_signal_handler for SIGTERM
            signal.signal(sig, lambda s, f: _handle_signal(signal.Signals(s)))

    # Initialize APScheduler (BackgroundScheduler in its own thread)
    job_queue = init_scheduler(loop)
    CronScheduler.add_system_jobs()
    logger.info("Scheduler ready")

    # Start concurrent tasks
    tasks = [
        asyncio.create_task(sender_loop(), name="sender_loop"),
        asyncio.create_task(bot.run(), name="telegram_bot"),
        asyncio.create_task(orchestrator.run(), name="orchestrator"),
        asyncio.create_task(orchestrator.watchdog(), name="watchdog"),
        asyncio.create_task(run_webhook_server(bot.message_queue), name="webhook_server"),
    ]

    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down…")
        orchestrator.stop()

        # Cancel all running tasks
        for t in tasks:
            t.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        # Close sessions with LLM summary (Phase 3+)
        try:
            from memory.sessions import SessionStore  # noqa: PLC0415
            await SessionStore().close_all_sessions()
        except ImportError:
            pass

        # Close databases
        from core.database import state_db, memory_db, journal_db  # noqa: PLC0415
        await state_db.close()
        await memory_db.close()
        await journal_db.close()

        logger.info("ARIA shut down cleanly")


def main() -> None:
    _configure_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="ARIA — Autonomous Resident Intelligence Agent")
    parser.add_argument("--setup", action="store_true", help="Run the onboarding wizard")
    args = parser.parse_args()

    if args.setup:
        import setup as setup_module  # noqa: PLC0415
        setup_module.run_wizard()
        return

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — exiting")
    except Exception:
        logger.exception("Fatal error in main loop")
        sys.exit(1)


if __name__ == "__main__":
    main()
