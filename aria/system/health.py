"""System health monitoring — psutil + Pi temperature + LLM reachability."""

import logging
import sys
from typing import Any

import psutil

logger = logging.getLogger(__name__)

# Alert thresholds — constants, not configurable at runtime
THRESHOLDS: dict[str, tuple[float, str]] = {
    "cpu_pct":  (90.0, "⚠️ CPU at {v}% — ARIA may be slow"),
    "ram_pct":  (85.0, "⚠️ RAM at {v}% — consider restarting"),
    "disk_pct": (90.0, "⚠️ Disk at {v}% — backup and prune data/"),
    "temp_c":   (80.0, "🌡️ Pi temperature {v}°C — check ventilation"),
}


class HealthMonitor:

    @staticmethod
    async def snapshot() -> dict[str, float]:
        """Capture a point-in-time health snapshot and write to health table."""
        from core.database import state_db

        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent if sys.platform != "win32" else psutil.disk_usage("C:\\").percent
        temp = _read_pi_temp()

        await state_db.execute(
            "INSERT INTO health(cpu_pct, ram_pct, disk_pct, temp_c) VALUES (?, ?, ?, ?)",
            (cpu, ram, disk, temp),
        )
        await state_db.commit()

        snapshot = {"cpu_pct": cpu, "ram_pct": ram, "disk_pct": disk, "temp_c": temp}
        await check_thresholds(snapshot)
        return snapshot

    @staticmethod
    async def check_llm() -> bool:
        """Verify LLM reachability with a 1-token completion and 5s timeout."""
        import asyncio
        from core.llm_client import llm_call
        from core.config import settings

        try:
            await asyncio.wait_for(
                llm_call(
                    model=settings.get_model("light"),
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    record_usage=False,
                ),
                timeout=5.0,
            )
            return True
        except Exception:
            return False

    @staticmethod
    async def check_browser_health() -> bool:
        """Open Chromium, navigate to example.com, verify title, close."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto("https://example.com", timeout=10000)
                title = await page.title()
                await browser.close()
                return "example" in title.lower()
        except Exception:
            logger.warning("Browser health check failed", exc_info=True)
            return False


def _read_pi_temp() -> float:
    """Read CPU temperature on Raspberry Pi / Linux. Returns 0.0 on Windows."""
    if sys.platform == "win32":
        return 0.0
    try:
        path = "/sys/class/thermal/thermal_zone0/temp"
        with open(path) as f:
            return float(f.read().strip()) / 1000.0
    except Exception:
        return 0.0


async def _cleanup_chromium_processes() -> None:
    """Safely kill orphaned Chromium processes by PID + start-time verification."""
    import psutil
    from core.database import state_db

    try:
        rows = await state_db.fetchall("SELECT pid, start_time FROM chromium_procs")
        pids_to_delete = []
        for row in rows:
            pid = row["pid"]
            stored_start = row["start_time"]
            try:
                proc = psutil.Process(pid)
                # Double-check: PID must belong to Chromium AND start time must match
                actual_start = proc.create_time()
                name = proc.name().lower()
                if ("chromium" in name or "chrome" in name) and abs(actual_start - stored_start) < 2.0:
                    proc.terminate()
                    logger.info("Terminated orphaned Chromium PID %d", pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass  # already dead or not ours
            pids_to_delete.append(pid)

        for pid in pids_to_delete:
            await state_db.execute("DELETE FROM chromium_procs WHERE pid=?", (pid,))
        if pids_to_delete:
            await state_db.commit()
    except Exception:
        logger.debug("Chromium cleanup skipped", exc_info=True)


async def check_thresholds(snapshot: dict[str, float]) -> None:
    """Fire Telegram alerts if any metric exceeds its threshold."""
    from tg.notify import send_text
    from core.config import settings

    for key, (threshold, msg_template) in THRESHOLDS.items():
        val = snapshot.get(key, 0.0)
        if val > threshold:
            try:
                await send_text(
                    settings.telegram_chat_id,
                    msg_template.format(v=round(val, 1)),
                )
            except Exception:
                logger.exception("Failed to send health alert for %s", key)
