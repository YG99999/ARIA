"""VNC handover — escalates to human when ARIA is blocked by a CAPTCHA or challenge.

On Linux/Pi: opens a VNC session and sends the Tailscale URL to Telegram.
On Windows: returns "not available".
"""

import logging
import sys

logger = logging.getLogger(__name__)


class HumanHandover:
    _active: bool = False
    _start_time: float | None = None

    @classmethod
    async def send_handover_link(cls, screenshot_b64: str | None = None) -> None:
        if sys.platform == "win32":
            from tg.notify import send_text
            from core.config import settings
            await send_text(settings.telegram_chat_id, "VNC handover not available on Windows.")
            return

        from core.config import settings
        from tg.notify import send_text

        ip = settings.tailscale_ip
        if ip:
            vnc_url = f"https://{ip}:6080/vnc.html"
        else:
            # Fallback: local LAN (plain HTTP only for LAN, never Tailscale)
            import socket
            local_ip = socket.gethostbyname(socket.gethostname())
            vnc_url = f"http://{local_ip}:6080/vnc.html"

        import time
        cls._active = True
        cls._start_time = time.time()

        msg = (
            f"🖥️ ARIA needs your help!\n\n"
            f"I'm stuck (CAPTCHA or complex interaction). Please take over:\n"
            f"{vnc_url}\n\n"
            f"When done, send /resume to let me continue."
        )

        if screenshot_b64:
            # Send screenshot first
            import base64
            import io
            try:
                img_bytes = base64.b64decode(screenshot_b64.split(",")[-1])
                await send_text(settings.telegram_chat_id, "Current screen state:")
                # TODO: send as photo via notify.send_photo()
            except Exception:
                pass

        await send_text(settings.telegram_chat_id, msg)

    @classmethod
    async def resume(cls) -> None:
        import time
        duration = time.time() - (cls._start_time or time.time())
        cls._active = False
        cls._start_time = None

        from system.journal import log as journal_log
        await journal_log("system", f"Human handover ended after {int(duration)}s")

    @classmethod
    def is_active(cls) -> bool:
        return cls._active
