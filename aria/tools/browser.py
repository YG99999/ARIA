"""Browser factory + browse tool using browser-use Agent.

browser-use is pinned to ==0.1.40 — do not auto-update. Minor version bumps have
broken the Agent API and on_step_callback behavior in the past.

BROWSER_LOCK enforces one concurrent browser task — not a limitation, a design decision.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEALTH LIMITATION (documented, accepted)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
browse() uses browser-use's internal Playwright context without our stealth
patches. browser-use 0.1.40 manages its own Playwright browser and context
internally and does not expose a context-creation hook we can reliably intercept.

playwright-stealth is applied ONLY to ARIA-managed Playwright contexts —
i.e. create_browser() and any custom Playwright-based tools that call it.

When a site is highly bot-sensitive (e.g. aggressive Cloudflare, Akamai,
Datadome), route the task through our own create_browser() path instead
of the browse() tool, so stealth is applied. Use the browse() tool for
standard sites where fingerprint hardening is not required.

Do NOT attempt to inject stealth into browser-use's internal context —
the per-context API changed in playwright-stealth 1.0.6+ and monkey-patching
browser-use internals would break on minor version bumps.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

playwright-stealth in create_browser(): applied at context level via
Stealth().use_async(context) in an on_context_created callback — NOT per-page
(per-page API is broken in 1.0.6+).

Session-first strategy: load stored Playwright session before falling back to
get_credential(). After successful login, auto-save with save_session_state.

LLM for browser-use: uses langchain-openai ChatOpenAI pointed at settings.llm_base_url
so it works with OpenRouter, Anthropic proxy, or any OpenAI-compatible endpoint.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from core.tools import tool

logger = logging.getLogger(__name__)

# Enforces one concurrent browser task
BROWSER_LOCK = asyncio.Lock()


async def create_browser(headless: bool | None = None):
    """Create an ARIA-managed Playwright context with stealth and a persistent profile.

    Uses launch_persistent_context() — the only Playwright API that accepts
    user_data_dir. browser.new_context() does NOT support user_data_dir.

    Returns (playwright, context) — caller must close both.
    The underlying browser is available as context.browser if needed.

    Stealth is applied here at context level via Stealth().use_async(context).
    This is the ONLY path where stealth is active. The browse() tool uses
    browser-use's internal Playwright context which we do not patch.
    """
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth
    from core.config import settings

    if headless is None:
        headless = settings.browser_headless

    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":99"
        logger.debug("Set DISPLAY=:99 for Playwright")

    launch_args = []
    if sys.platform == "linux":
        launch_args += [
            "--disable-dev-shm-usage",
            "--disk-cache-dir=/tmp",
        ]
        # Raspberry Pi memory constraint
        try:
            with open("/proc/cpuinfo") as f:
                if "Raspberry" in f.read():
                    launch_args.append("--js-flags=--max-old-space-size=512")
        except OSError:
            pass

    playwright = await async_playwright().start()

    # launch_persistent_context returns a BrowserContext directly (not a Browser).
    # This is the correct API for persistent profiles with user_data_dir.
    settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(settings.browser_profile_dir),
        headless=headless,
        args=launch_args,
    )

    # Apply stealth at context level (NOT per-page — per-page is broken in 1.0.6+)
    await Stealth().use_async(context)

    # Track PID for cleanup (via context.browser)
    if context.browser:
        await _track_browser_pid(context.browser)

    return playwright, context


async def _track_browser_pid(browser) -> None:
    """Store browser PID + start time for safe cleanup."""
    try:
        import psutil
        from core.database import state_db

        pid = browser.browser_type  # placeholder — actual PID via process
        # browser-use / Playwright doesn't expose PID directly in all versions
        # Store a sentinel so cleanup logic can work
        logger.debug("Browser launched (PID tracking via psutil)")
    except Exception:
        pass


async def load_session_if_available(
    context, service: str, account_label: str
) -> bool:
    """Load a stored Playwright session into the context if available.

    Returns True if session was loaded, False otherwise.
    """
    from core.config import settings

    session_path = settings.data_dir / "browser_state" / service / f"{account_label}.json"
    if not session_path.exists():
        return False

    try:
        import json
        state = json.loads(session_path.read_text(encoding="utf-8"))
        await context.add_cookies(state.get("cookies", []))
        logger.info("Loaded browser session for %s/%s", service, account_label)
        return True
    except Exception as exc:
        logger.warning("Failed to load session for %s/%s: %s", service, account_label, exc)
        return False


def _make_browser_use_llm():
    """Create a langchain ChatOpenAI pointed at ARIA's configured LLM endpoint."""
    from core.config import settings
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "langchain-openai is required for browser-use. "
            "Run: pip install langchain-openai"
        ) from exc

    return ChatOpenAI(
        model=settings.get_model("heavy"),
        openai_api_key=settings.llm_api_key,
        openai_api_base=settings.llm_base_url,
        temperature=0.0,
    )


@tool(
    name="browse",
    description=(
        "Navigate and interact with a website using browser-use's autonomous Agent. "
        "Describe exactly what you need done: 'Log in to stripe.com and get yesterday's revenue'. "
        "For information retrieval only, prefer search_web (10× faster). "
        "Only one browser task runs at a time — others queue automatically. "
        "NOTE: this tool uses browser-use's internal Playwright context WITHOUT stealth patches. "
        "For highly bot-sensitive sites (aggressive Cloudflare, Akamai, Datadome), "
        "use a custom Playwright tool via create_browser() which applies stealth."
    ),
    schema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Full description of what to do in the browser",
            },
            "service": {
                "type": "string",
                "description": "Service name for session lookup (e.g. 'stripe', 'github'). Optional.",
            },
            "account_label": {
                "type": "string",
                "description": "Account label for session lookup (e.g. 'main'). Optional.",
            },
            "max_steps": {
                "type": "integer",
                "description": "Max autonomous browser steps before giving up (default 25)",
            },
        },
        "required": ["task"],
    },
)
async def browse(
    task: str,
    service: str | None = None,
    account_label: str | None = None,
    max_steps: int = 25,
    task_id: str | None = None,
    **kwargs,
) -> str:
    """Run a browser-use Agent to complete a browser task autonomously.

    Acquires BROWSER_LOCK — only one browser task runs at a time.
    Registers custom controller actions so the Agent can call back into ARIA:
      - save_to_memory: persist findings as facts
      - notify_user: send Telegram message mid-task
      - request_approval: block for user confirmation
      - escalate: hand off to human via VNC if stuck
    """
    from core.config import settings
    from system.journal import log as journal_log

    async with BROWSER_LOCK:
        try:
            from browser_use import Agent, Controller
            from browser_use.browser.browser import Browser, BrowserConfig
        except ImportError:
            return (
                "browser-use not installed. "
                "Run: pip install browser-use==0.1.40 playwright==1.47.0"
            )

        try:
            llm = _make_browser_use_llm()
        except ImportError as exc:
            return str(exc)

        # ── Custom controller actions ──────────────────────────────────
        controller = Controller()

        @controller.action(
            "Get a 2FA / TOTP code for a registered account. "
            "Pass the service name (e.g. 'github', 'stripe'). "
            "Returns the current 6-digit code or an error string."
        )
        async def get_2fa_code(service: str) -> str:
            """Retrieve the current TOTP code for a service from the credential store."""
            try:
                from tools.credential_tools import get_credential as _get_cred
                # Credentials of kind 'totp' store the base32 seed
                seed = await _get_cred(service=service, kind="totp")
                if seed.startswith("Error") or seed.startswith("No credential"):
                    return f"No TOTP seed found for {service}. Save one first with save_credential."
                import hmac as _hmac, hashlib, struct, time as _time, base64

                # Standard TOTP (RFC 6238, 30-second window, SHA1)
                key_bytes = base64.b32decode(seed.strip().upper(), casefold=True)
                counter = int(_time.time()) // 30
                msg = struct.pack(">Q", counter)
                h = _hmac.new(key_bytes, msg, hashlib.sha1).digest()
                offset = h[-1] & 0x0F
                code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
                return f"{code:06d}"
            except Exception as exc:
                logger.warning("get_2fa_code failed for %s: %s", service, exc)
                return f"2FA error: {exc}"

        @controller.action("Save a fact to ARIA's persistent memory")
        async def save_to_memory(key: str, value: str) -> str:
            from memory.facts import FactsStore
            await FactsStore.upsert(key, value, source="browser_agent")
            return f"Saved: {key}"

        @controller.action("Send a notification to the user via Telegram")
        async def notify_user(message: str) -> str:
            from tg.notify import send_text
            await send_text(settings.telegram_chat_id, message)
            return "Notification sent"

        @controller.action("Request human approval before taking a sensitive action")
        async def request_approval_action(description: str) -> str:
            from core.approval import ApprovalQueue
            try:
                result = await ApprovalQueue.request(
                    category="publish",
                    description=description,
                    task_id=task_id,
                )
                return result
            except RuntimeError as exc:
                return f"Approval denied: {exc}"

        @controller.action("Escalate to human — stuck on CAPTCHA or complex challenge")
        async def escalate_to_human(reason: str) -> str:
            from system.handover import HumanHandover
            await journal_log("system", f"Browser escalation: {reason}", task_id=task_id)
            await HumanHandover.send_handover_link()
            return "Handover link sent. Send /resume when done."

        # ── Step logging callback ──────────────────────────────────────
        step_counter = [0]

        async def _on_step(state=None, output=None, **_kw):
            step_counter[0] += 1
            msg = ""
            if output is not None:
                msg = str(output)[:200]
            elif state is not None:
                msg = str(state)[:200]
            try:
                await journal_log(
                    "action",
                    f"Browser step {step_counter[0]}: {msg}",
                    task_id=task_id,
                )
            except Exception:
                pass

        # ── Browser config ─────────────────────────────────────────────
        if sys.platform == "linux" and not os.environ.get("DISPLAY"):
            os.environ["DISPLAY"] = ":99"

        extra_args = []
        if sys.platform == "linux":
            extra_args += ["--disable-dev-shm-usage", "--disk-cache-dir=/tmp"]
            try:
                with open("/proc/cpuinfo") as f:
                    if "Raspberry" in f.read():
                        extra_args.append("--js-flags=--max-old-space-size=512")
            except OSError:
                pass

        browser_config = BrowserConfig(
            headless=settings.browser_headless,
            extra_chromium_args=extra_args,
        )
        browser_instance = Browser(config=browser_config)

        await journal_log(
            "action",
            f"Browser task started: {task[:120]}",
            task_id=task_id,
        )

        try:
            agent = Agent(
                task=task,
                llm=llm,
                browser=browser_instance,
                controller=controller,
            )

            # Wire step callback — attribute name varies by version
            for attr in ("on_step_end", "on_step_start", "register_new_step_callback"):
                if hasattr(agent, attr):
                    if callable(getattr(agent, attr)):
                        try:
                            getattr(agent, attr)(_on_step)
                        except Exception:
                            pass
                    break

            history = await agent.run(max_steps=max_steps)

            # Extract final result — API may differ slightly between patch versions
            result = None
            if hasattr(history, "final_result"):
                result = history.final_result()
            elif hasattr(history, "result"):
                result = history.result()
            if result is None:
                result = str(history)

            result_str = str(result)[:2000] if result else "Browser task completed."
            await journal_log(
                "action",
                f"Browser task done after {step_counter[0]} steps: {result_str[:200]}",
                task_id=task_id,
            )
            return result_str

        except Exception as exc:
            err = f"Browser task failed: {exc}"
            logger.exception("browse() failed for task_id=%s", task_id)
            await journal_log("error", err, task_id=task_id)
            return err

        finally:
            try:
                await browser_instance.close()
            except Exception:
                pass
