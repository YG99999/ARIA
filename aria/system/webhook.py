"""Webhook receiver — aiohttp server on port 8765.

Started as asyncio.create_task() in main.py alongside the Telegram bot.
Same event loop as everything else. Incoming events are HMAC-verified and
tagged EXTERNAL (never trusted as instructions).

Tailscale exposes port 8765 with zero firewall config.
"""

import hashlib
import hmac
import json
import logging

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

WEBHOOK_PORT = 8765
_app: web.Application | None = None


async def run_webhook_server(message_queue) -> None:
    """Start the aiohttp webhook server. Pass the orchestrator's message queue."""
    global _app
    _app = web.Application()
    _app["message_queue"] = message_queue

    _app.router.add_post("/webhook/{source}", handle_webhook)
    _app.router.add_get("/webhook/health", handle_health)

    runner = web.AppRunner(_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info("Webhook server listening on port %d", WEBHOOK_PORT)

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await runner.cleanup()


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_webhook(request: web.Request) -> web.Response:
    import asyncio
    from core.config import settings
    from core.context_builder import TrustLevel, tag, check_for_injection
    from system.journal import log as journal_log

    source = request.match_info["source"]
    body = await request.read()

    # HMAC-SHA256 verification
    secret = settings.webhook_secrets.get(source)
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Webhook signature mismatch from source=%s", source)
            return web.Response(status=401, text="Invalid signature")

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError:
        payload = {"raw": body.decode()[:500]}

    payload_str = json.dumps(payload, indent=2)[:1000]

    # Check for injection in webhook content
    if check_for_injection(payload_str, f"webhook:{source}"):
        await journal_log(
            "error",
            f"Suspected injection in webhook from source={source}",
        )
        from tg.notify import send_text
        await send_text(
            settings.telegram_chat_id,
            f"⚠️ Suspected injection in webhook from '{source}'. Payload ignored.",
        )
        return web.Response(status=200, text="ok")

    # Tag as EXTERNAL and enqueue
    tagged_payload = tag(
        f"Webhook from {source}:\n{payload_str}",
        TrustLevel.EXTERNAL,
        source=source,
    )

    await journal_log("system", f"Webhook received from {source}")

    # Enqueue as a text message to the orchestrator
    from tg.bot import TextMessage
    queue = request.app["message_queue"]
    msg = TextMessage(text=tagged_payload, update=None)
    await queue.put(msg)

    return web.Response(status=200, text="ok")


import asyncio  # noqa: E402 — needed for asyncio.Event() in run_webhook_server
