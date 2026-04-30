"""Computer-use tools — screenshot, mouse, keyboard via pyautogui.

Vision routing: take_screenshot results MUST go to the smart model tier.
WorkerAgent._run_single_tool() detects base64 PNG results and upgrades the model.

Platform guard: set DISPLAY=:99 on Linux (requires Xvfb running).
On Windows/macOS, use native display — do not set DISPLAY.
"""

import base64
import io
import logging
import os
import sys
from typing import Literal

from core.tools import tool

logger = logging.getLogger(__name__)


def _setup_display() -> None:
    """Set DISPLAY for headless Linux. No-op on Windows/macOS."""
    if sys.platform != "linux":
        return
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":99"
        logger.debug("Set DISPLAY=:99 for pyautogui")


def _import_pyautogui():
    _setup_display()
    import pyautogui
    pyautogui.FAILSAFE = False
    return pyautogui


@tool(
    name="take_screenshot",
    description=(
        "Take a screenshot of the current screen. "
        "Returns a base64-encoded PNG. The LLM will automatically use vision to analyze it."
    ),
    schema={
        "type": "object",
        "properties": {},
    },
)
async def take_screenshot(**kwargs) -> str:
    import asyncio

    def _capture():
        pag = _import_pyautogui()
        img = pag.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    loop = asyncio.get_running_loop()
    b64 = await loop.run_in_executor(None, _capture)
    return f"data:image/png;base64,{b64}"


@tool(
    name="mouse_click",
    description="Click a screen coordinate with the specified mouse button.",
    schema={
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate (pixels)"},
            "y": {"type": "integer", "description": "Y coordinate (pixels)"},
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Mouse button (default: left)",
            },
        },
        "required": ["x", "y"],
    },
)
async def mouse_click(x: int, y: int, button: str = "left", **kwargs) -> str:
    import asyncio

    def _click():
        pag = _import_pyautogui()
        pag.click(x, y, button=button)

    await asyncio.get_running_loop().run_in_executor(None, _click)
    return f"Clicked {button} at ({x}, {y})"


@tool(
    name="keyboard_type",
    description="Type text at the current cursor position.",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type"},
            "press_enter": {"type": "boolean", "description": "Press Enter after typing (default false)"},
        },
        "required": ["text"],
    },
)
async def keyboard_type(text: str, press_enter: bool = False, **kwargs) -> str:
    import asyncio

    def _type():
        pag = _import_pyautogui()
        pag.typewrite(text, interval=0.02)
        if press_enter:
            pag.press("enter")

    await asyncio.get_running_loop().run_in_executor(None, _type)
    return f"Typed {len(text)} chars" + (" + Enter" if press_enter else "")


@tool(
    name="key_press",
    description="Press one or more keys (e.g. 'ctrl+c', 'enter', 'tab', 'escape').",
    schema={
        "type": "object",
        "properties": {
            "keys": {
                "type": "string",
                "description": "Key combination, e.g. 'ctrl+c', 'alt+tab', 'enter'",
            },
        },
        "required": ["keys"],
    },
)
async def key_press(keys: str, **kwargs) -> str:
    import asyncio

    def _press():
        pag = _import_pyautogui()
        parts = keys.lower().split("+")
        if len(parts) == 1:
            pag.press(parts[0])
        else:
            pag.hotkey(*parts)

    await asyncio.get_running_loop().run_in_executor(None, _press)
    return f"Pressed: {keys}"
