"""Rate-limited Telegram outbound sender.

ALL outgoing Telegram messages go through send_text() / send_document() here.
Never call bot.send_message() directly — that bypasses the rate limiter.

The sender_loop() coroutine must be started as asyncio.create_task() at startup.
It drains _send_queue at max 20 msg/sec (Telegram's limit is ~30/sec; we stay under).
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
_bot: Any = None  # set by TelegramBot after construction


def set_bot(bot: Any) -> None:
    """Called by TelegramBot.__init__() to register the Application instance."""
    global _bot
    _bot = bot


async def sender_loop() -> None:
    """Drain the outbound queue at ≤20 messages/second. Run as asyncio.create_task()."""
    while True:
        try:
            msg = await _send_queue.get()
            if _bot is None:
                logger.warning("sender_loop: bot not set yet — dropping message")
                _send_queue.task_done()
                continue
            method = msg.pop("_method", "send_message")
            try:
                fn = getattr(_bot.bot, method)
                await fn(**msg)
            except Exception:
                logger.exception("Failed to send Telegram message via %s", method)
            finally:
                _send_queue.task_done()
            await asyncio.sleep(0.05)  # max 20 msg/sec
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Unexpected error in sender_loop")


async def send_text(
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: Any = None,
    **kwargs: Any,
) -> None:
    """Enqueue a plain-text message."""
    msg: dict[str, Any] = {
        "_method": "send_message",
        "chat_id": chat_id,
        "text": text[:4096],  # Telegram max message length
    }
    if parse_mode:
        msg["parse_mode"] = parse_mode
    if reply_markup:
        msg["reply_markup"] = reply_markup
    msg.update(kwargs)
    await _send_queue.put(msg)


async def send_document(
    chat_id: int,
    document: Any,
    caption: str | None = None,
    **kwargs: Any,
) -> None:
    """Enqueue a document/file send."""
    msg: dict[str, Any] = {
        "_method": "send_document",
        "chat_id": chat_id,
        "document": document,
    }
    if caption:
        msg["caption"] = caption[:1024]
    msg.update(kwargs)
    await _send_queue.put(msg)


async def send_photo(chat_id: int, photo: Any, caption: str | None = None, **kwargs: Any) -> None:
    """Enqueue a photo send."""
    msg: dict[str, Any] = {"_method": "send_photo", "chat_id": chat_id, "photo": photo}
    if caption:
        msg["caption"] = caption[:1024]
    msg.update(kwargs)
    await _send_queue.put(msg)


# ------------------------------------------------------------------
# UX mode helpers
# ------------------------------------------------------------------

BEGINNER_STRINGS: dict[str, str] = {
    "status_idle": "I'm idle and ready.",
    "status_working": "I'm working on: {title}.",
    "task_done": "Done! Here's what I did: {summary}",
    "task_error": "I couldn't finish that. I saved where I got to.",
    "approval_request": "I need your approval to {description}.",
    "injection_detected": "A website showed suspicious instructions; I ignored them.",
    "progress": "{step}",
}

EXPERT_STRINGS: dict[str, str] = {
    "status_idle": "Idle. No active tasks.",
    "status_working": "[{task_id}] {kind} agent running: {title}",
    "task_done": "[{task_id}] {verdict} in {step_count} steps. Use /task {task_id} for details.",
    "task_error": "[{task_id}] FAILED at {failure_stage}. Exception: {exc_type}. See /journal.",
    "approval_request": "Approval required [{category}]: {description}\nContext: {context}\nTimeout: {timeout}m",
    "injection_detected": "⚠️ Suspected injection in {source}. Signal: '{signal}'. See /trust.",
    "progress": "[step {step_num}] {step}",
}


def ux_text(key: str, ux_mode: str = "beginner", **kwargs: Any) -> str:
    """Return a user-facing string formatted for the current UX mode."""
    strings = EXPERT_STRINGS if ux_mode == "expert" else BEGINNER_STRINGS
    template = strings.get(key, key)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        return template
