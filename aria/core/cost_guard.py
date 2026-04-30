"""Budget enforcement — called inside llm_client.llm_call() before every API call.

Raises BudgetExceededError at daily_usd_hard_stop.
Sends an alert-only Telegram notification at daily_usd_alert.
"""

import logging

logger = logging.getLogger(__name__)

_budget_exceeded: bool = False
_alert_sent_today: str | None = None  # date string "YYYY-MM-DD"


class BudgetExceededError(Exception):
    """Raised when the daily LLM spend hard-stop is hit."""


async def check() -> None:
    """Check budget before an LLM call. Raises BudgetExceededError if over limit."""
    global _budget_exceeded, _alert_sent_today

    if _budget_exceeded:
        raise BudgetExceededError("Daily LLM budget hard-stop reached — no new calls until tomorrow")

    try:
        from core.config import settings
        from core.database import state_db

        budget = settings.budget
        daily_alert = float(budget.get("daily_usd_alert", 2.0))
        daily_hard_stop = float(budget.get("daily_usd_hard_stop", 10.0))

        row = await state_db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM llm_usage WHERE date = date('now')"
        )
        today_spend = float(row["total"]) if row else 0.0

        if today_spend >= daily_hard_stop:
            _budget_exceeded = True
            logger.error(
                "Daily LLM budget hard-stop reached: $%.4f >= $%.2f",
                today_spend, daily_hard_stop,
            )
            # Notify user (non-blocking — best effort)
            try:
                from tg.notify import send_text  # noqa: PLC0415
                from core.config import settings as s  # noqa: PLC0415
                await send_text(
                    s.telegram_chat_id,
                    f"⛔ Daily LLM budget hard-stop reached (${today_spend:.2f} / ${daily_hard_stop:.2f}). "
                    f"No more AI calls until tomorrow.",
                )
            except Exception:
                pass
            raise BudgetExceededError(
                f"Daily spend ${today_spend:.4f} >= hard-stop ${daily_hard_stop:.2f}"
            )

        import datetime
        today_str = datetime.date.today().isoformat()
        if today_spend >= daily_alert and _alert_sent_today != today_str:
            _alert_sent_today = today_str
            logger.warning("Daily LLM spend alert: $%.4f >= $%.2f", today_spend, daily_alert)
            try:
                from tg.notify import send_text  # noqa: PLC0415
                from core.config import settings as s  # noqa: PLC0415
                await send_text(
                    s.telegram_chat_id,
                    f"⚠️ Daily LLM spend alert: ${today_spend:.2f} of ${daily_hard_stop:.2f} used today.",
                )
            except Exception:
                pass

    except BudgetExceededError:
        raise
    except Exception:
        logger.debug("Budget check failed (non-fatal)", exc_info=True)


def reset_for_new_day() -> None:
    """Called by the orchestrator at midnight to re-enable LLM calls."""
    global _budget_exceeded, _alert_sent_today
    _budget_exceeded = False
    _alert_sent_today = None


async def get_spend_summary() -> dict[str, float]:
    """Return today's and this month's spend for /budget command."""
    from core.database import state_db

    today_row = await state_db.fetchone(
        "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM llm_usage WHERE date = date('now')"
    )
    month_row = await state_db.fetchone(
        "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM llm_usage "
        "WHERE date >= date('now', 'start of month')"
    )
    return {
        "today_usd": float(today_row["total"]) if today_row else 0.0,
        "month_usd": float(month_row["total"]) if month_row else 0.0,
    }
