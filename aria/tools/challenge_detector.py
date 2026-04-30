"""Challenge detector — identifies Cloudflare, reCAPTCHA, hCaptcha blocks in browser pages.

CaptchaSolver: optional CapSolver REST API integration.
  - Enabled by setting CAPSOLVER_KEY in .env
  - Also requires the user to run /captcha on in Telegram (stored as fact 'captcha_solver')
  - Supports: recaptcha_v2, recaptcha_v3, hcaptcha, cloudflare_turnstile
  - https://docs.capsolver.com/
"""

import asyncio
import logging

from core.tools import tool

logger = logging.getLogger(__name__)

# CapSolver API base
_CAPSOLVER_BASE = "https://api.capsolver.com"

# Signals that indicate a challenge/block page
BLOCK_SIGNALS = [
    "cloudflare",
    "cf-challenge",
    "attention required",
    "ddos protection",
    "recaptcha",
    "hcaptcha",
    "are you human",
    "verify you are human",
    "robot check",
    "access denied",
    "403 forbidden",
    "just a moment",
]


def detect_challenge(page_content: str) -> tuple[bool, str | None]:
    """Check page content for block signals.

    Returns (is_blocked, challenge_type | None).
    """
    lower = page_content.lower()
    for signal in BLOCK_SIGNALS:
        if signal in lower:
            kind = (
                "cloudflare" if "cloudflare" in signal or "cf-challenge" in signal or "just a moment" in signal
                else "recaptcha" if "recaptcha" in signal
                else "hcaptcha" if "hcaptcha" in signal
                else "generic_block"
            )
            return True, kind
    return False, None


async def _capsolver_enabled() -> bool:
    """Return True if CapSolver is configured and user has enabled it."""
    from core.config import settings
    if not settings.capsolver_key:
        return False
    try:
        from memory.facts import FactsStore
        val = await FactsStore.get("captcha_solver")
        return val == "1"
    except Exception:
        return False


async def _capsolver_create_task(task_payload: dict) -> str | None:
    """Submit a task to CapSolver and return the taskId, or None on failure."""
    import aiohttp
    from core.config import settings

    payload = {
        "clientKey": settings.capsolver_key,
        "task": task_payload,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_CAPSOLVER_BASE}/createTask",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if data.get("errorId", 1) == 0:
                    return data.get("taskId")
                logger.warning("CapSolver createTask error: %s", data.get("errorDescription"))
    except Exception as exc:
        logger.warning("CapSolver createTask failed: %s", exc)
    return None


async def _capsolver_get_result(task_id: str, max_wait: int = 120) -> str | None:
    """Poll CapSolver for task result. Returns the solution token or None."""
    import aiohttp
    from core.config import settings

    payload = {"clientKey": settings.capsolver_key, "taskId": task_id}
    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_CAPSOLVER_BASE}/getTaskResult",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()
                    if data.get("errorId", 1) != 0:
                        logger.warning("CapSolver getTaskResult error: %s", data.get("errorDescription"))
                        return None
                    status = data.get("status")
                    if status == "ready":
                        sol = data.get("solution", {})
                        # gRecaptchaResponse for recaptcha; token for turnstile/hcaptcha
                        return (
                            sol.get("gRecaptchaResponse")
                            or sol.get("token")
                            or sol.get("userAgent")  # fallback
                        )
                    # status == "processing" → keep polling
        except Exception as exc:
            logger.warning("CapSolver getTaskResult failed: %s", exc)
            return None
    logger.warning("CapSolver timed out waiting for task %s", task_id)
    return None


class CaptchaSolver:
    """CapSolver REST API integration for automated CAPTCHA solving.

    Disabled by default — requires CAPSOLVER_KEY env var and /captcha on command.
    """

    @staticmethod
    async def solve_recaptcha_v2(site_key: str, page_url: str) -> str | None:
        """Solve reCAPTCHA v2. Returns the gRecaptchaResponse token or None."""
        if not await _capsolver_enabled():
            logger.debug("CaptchaSolver disabled — skipping recaptcha_v2")
            return None

        task_payload = {
            "type": "ReCaptchaV2TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        task_id = await _capsolver_create_task(task_payload)
        if not task_id:
            return None
        logger.info("CapSolver recaptcha_v2 task %s submitted", task_id)
        return await _capsolver_get_result(task_id)

    @staticmethod
    async def solve_recaptcha_v3(
        site_key: str,
        page_url: str,
        action: str = "verify",
        min_score: float = 0.5,
    ) -> str | None:
        """Solve reCAPTCHA v3. Returns the token or None."""
        if not await _capsolver_enabled():
            return None

        task_payload = {
            "type": "ReCaptchaV3TaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "pageAction": action,
            "minScore": min_score,
        }
        task_id = await _capsolver_create_task(task_payload)
        if not task_id:
            return None
        logger.info("CapSolver recaptcha_v3 task %s submitted", task_id)
        return await _capsolver_get_result(task_id)

    @staticmethod
    async def solve_hcaptcha(site_key: str, page_url: str) -> str | None:
        """Solve hCaptcha. Returns the token or None."""
        if not await _capsolver_enabled():
            return None

        task_payload = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        task_id = await _capsolver_create_task(task_payload)
        if not task_id:
            return None
        logger.info("CapSolver hcaptcha task %s submitted", task_id)
        return await _capsolver_get_result(task_id)

    @staticmethod
    async def solve_cloudflare_turnstile(site_key: str, page_url: str) -> str | None:
        """Solve Cloudflare Turnstile. Returns the token or None."""
        if not await _capsolver_enabled():
            return None

        task_payload = {
            "type": "AntiTurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        task_id = await _capsolver_create_task(task_payload)
        if not task_id:
            return None
        logger.info("CapSolver turnstile task %s submitted", task_id)
        return await _capsolver_get_result(task_id)

    @staticmethod
    async def auto_solve(challenge_type: str, site_key: str, page_url: str) -> str | None:
        """Dispatch to the appropriate solver based on detected challenge type."""
        if challenge_type == "recaptcha":
            return await CaptchaSolver.solve_recaptcha_v2(site_key, page_url)
        elif challenge_type == "hcaptcha":
            return await CaptchaSolver.solve_hcaptcha(site_key, page_url)
        elif challenge_type == "cloudflare":
            return await CaptchaSolver.solve_cloudflare_turnstile(site_key, page_url)
        else:
            logger.warning("No auto-solver for challenge type %r", challenge_type)
            return None


@tool(
    name="escalate",
    description=(
        "Escalate to the human owner when stuck on a CAPTCHA or complex challenge. "
        "Takes a screenshot and sends a VNC handover link via Telegram."
    ),
    schema={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why escalation is needed"},
        },
        "required": ["reason"],
    },
)
async def escalate(reason: str, task_id: str | None = None, **kwargs) -> str:
    from system.handover import HumanHandover
    from system.journal import log as journal_log

    await journal_log("system", f"Escalating to human: {reason}", task_id=task_id)

    # Try to take a screenshot before escalating
    screenshot = None
    try:
        from tools.computer_use import take_screenshot
        screenshot = await take_screenshot()
    except Exception:
        pass

    await HumanHandover.send_handover_link(screenshot_b64=screenshot)
    return f"Escalated to human. Reason: {reason}. Send /resume when done."
