"""Approval queue with asyncio.Event for blocking agent execution.

The asyncio.Event objects MUST be stored in this module-level dict (_pending_events).
They cannot be in the agent's local scope — the Telegram callback handler runs in a
different coroutine and needs to reach the same Event object across async boundaries.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Timeout in minutes per category
APPROVAL_CATEGORIES: dict[str, int] = {
    "spend_money": 10,
    "send_message": 30,
    "delete_file": 5,
    "publish": 30,
    "modify_core_code": 60,
}

# Module-level event store — key: approval_id, value: asyncio.Event
_pending_events: dict[str, asyncio.Event] = {}
_pending_results: dict[str, str] = {}  # "approved" | "denied" | "expired"


class ApprovalQueue:

    @staticmethod
    async def request(
        category: str,
        description: str,
        context_text: str | None = None,
        task_id: str | None = None,
    ) -> str:
        """Block the calling agent until the user approves or denies.

        Returns "approved" or raises RuntimeError("denied") / RuntimeError("expired").
        """
        from core.database import state_db
        from core.config import settings
        from tg.notify import send_text

        approval_id = str(uuid.uuid4())[:8]
        timeout_minutes = APPROVAL_CATEGORIES.get(category, 30)
        timeout_at = (datetime.utcnow() + timedelta(minutes=timeout_minutes)).isoformat()

        await state_db.execute(
            """INSERT INTO approvals(id, task_id, category, description, context, timeout_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (approval_id, task_id, category, description, context_text, timeout_at),
        )
        await state_db.commit()

        event = asyncio.Event()
        _pending_events[approval_id] = event

        # Build Telegram inline keyboard
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny:{approval_id}"),
            ]
        ])

        context_line = f"\n\nContext: {context_text[:200]}" if context_text else ""
        msg = (
            f"⚠️ Approval required [{category}]\n\n"
            f"{description}{context_line}\n\n"
            f"Timeout: {timeout_minutes} minutes\n"
            f"ID: {approval_id}"
        )

        try:
            await send_text(settings.telegram_chat_id, msg, reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to send approval request")

        # Block until approved/denied/expired
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_minutes * 60)
        except asyncio.TimeoutError:
            _pending_results[approval_id] = "expired"

        result = _pending_results.pop(approval_id, "expired")
        _pending_events.pop(approval_id, None)

        # Update DB
        await state_db.execute(
            "UPDATE approvals SET status=?, decided_at=datetime('now') WHERE id=?",
            (result, approval_id),
        )
        await state_db.commit()

        if result == "approved":
            return "approved"
        raise RuntimeError(f"Approval {approval_id} {result}")


def approve(approval_id: str) -> None:
    """Called by the Telegram callback handler when the user taps Approve."""
    _pending_results[approval_id] = "approved"
    if approval_id in _pending_events:
        _pending_events[approval_id].set()
    else:
        logger.warning("approve() called for unknown approval_id=%s", approval_id)


def deny(approval_id: str) -> None:
    """Called by the Telegram callback handler when the user taps Deny."""
    _pending_results[approval_id] = "denied"
    if approval_id in _pending_events:
        _pending_events[approval_id].set()
    else:
        logger.warning("deny() called for unknown approval_id=%s", approval_id)


async def check_expired() -> None:
    """Called each orchestrator tick to auto-expire timed-out approvals."""
    from core.database import state_db
    from core.config import settings
    from tg.notify import send_text

    rows = await state_db.fetchall(
        "SELECT id, description FROM approvals "
        "WHERE status='pending' AND timeout_at < datetime('now')"
    )
    for row in rows:
        aid = row["id"]
        _pending_results[aid] = "expired"
        if aid in _pending_events:
            _pending_events[aid].set()
        await state_db.execute(
            "UPDATE approvals SET status='expired', decided_at=datetime('now') WHERE id=?",
            (aid,),
        )
        try:
            await send_text(
                settings.telegram_chat_id,
                f"⏰ Approval [{aid}] expired: {row['description'][:80]}",
            )
        except Exception:
            pass

    if rows:
        await state_db.commit()
