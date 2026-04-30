"""Telegram bot — Application setup, auth gating, message ingestion.

Only the authorized TELEGRAM_CHAT_ID can interact with ARIA.
Unauthorized messages are silently dropped.

Incoming messages are enqueued to the orchestrator's message_queue — the bot
does NOT call the orchestrator directly. This keeps the bot deaf-proof: ARIA
can still receive /status and conversational messages while an agent runs.

Photo/document messages are wrapped in FileMessage alongside text.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import tg.commands as commands_module
from core.config import settings
from tg.notify import set_bot

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Message types
# ------------------------------------------------------------------


@dataclass
class TextMessage:
    text: str
    update: Any = field(repr=False)


@dataclass
class FileMessage:
    content: bytes
    mime_type: str
    prompt: str | None
    update: Any = field(repr=False)


# ------------------------------------------------------------------
# Bot class
# ------------------------------------------------------------------


class TelegramBot:
    def __init__(self) -> None:
        self.app: Application = (
            Application.builder()
            .token(settings.telegram_token)
            .build()
        )
        set_bot(self.app)

        # Shared queue with the orchestrator
        self.message_queue: asyncio.Queue[TextMessage | FileMessage] = asyncio.Queue()

        self._register_handlers()

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self.app

        # Slash commands — bypass orchestrator, direct SQLite reads
        for cmd, handler_fn in commands_module.COMMAND_HANDLERS.items():
            app.add_handler(CommandHandler(cmd, self._auth_wrap(handler_fn)))

        # Approval callback buttons
        app.add_handler(CallbackQueryHandler(self._auth_wrap(self._handle_callback_query)))

        # Text messages → enqueue for orchestrator
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._auth_wrap(self._handle_text),
            )
        )

        # Photo + document messages → FileMessage
        app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.ALL,
                self._auth_wrap(self._handle_file),
            )
        )

    # ------------------------------------------------------------------
    # Auth gate
    # ------------------------------------------------------------------

    def _auth_wrap(self, fn: Any) -> Any:
        """Wrap a handler so it silently ignores unauthorized senders."""
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id != settings.telegram_chat_id:
                logger.debug("Dropped message from unauthorized chat_id=%s", chat_id)
                return
            await fn(update, context)
        return wrapper

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip()
        if not text:
            return
        logger.debug("Received text: %r", text[:80])
        await self.message_queue.put(TextMessage(text=text, update=update))

    async def _handle_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        caption = (msg.caption or "").strip() if msg else None

        if msg.photo:
            photo = msg.photo[-1]  # largest resolution
            file = await context.bot.get_file(photo.file_id)
            content = await file.download_as_bytearray()
            mime_type = "image/jpeg"
        elif msg.document:
            doc = msg.document
            file = await context.bot.get_file(doc.file_id)
            content = await file.download_as_bytearray()
            mime_type = doc.mime_type or "application/octet-stream"
        else:
            return

        await self.message_queue.put(
            FileMessage(
                content=bytes(content),
                mime_type=mime_type,
                prompt=caption,
                update=update,
            )
        )

    async def _handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline keyboard button presses (approve/deny)."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data.startswith("approve:"):
            approval_id = data[len("approve:"):]
            from core.approval import approve  # noqa: PLC0415
            approve(approval_id)
            await query.edit_message_text(f"✅ Approved [{approval_id}]")
        elif data.startswith("deny:"):
            approval_id = data[len("deny:"):]
            from core.approval import deny  # noqa: PLC0415
            deny(approval_id)
            await query.edit_message_text(f"❌ Denied [{approval_id}]")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start Telegram polling loop. Runs until cancelled."""
        logger.info("Starting Telegram polling")
        async with self.app:
            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)
            try:
                # Keep running until cancelled
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
            finally:
                await self.app.updater.stop()
                await self.app.stop()
        logger.info("Telegram bot stopped")
