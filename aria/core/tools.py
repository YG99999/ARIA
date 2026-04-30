"""Tool registry — @tool decorator and discovery.

Usage:
    from core.tools import tool

    @tool(
        name="run_shell_command",
        description="Run a shell command and return stdout/stderr.",
        schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
            },
            "required": ["command"],
        },
    )
    async def run_shell_command(command: str) -> str:
        ...

Discovery:
    from core.tools import discover_tools, get_all_tools
    discover_tools()  # imports all tools/*.py and skills/*.py modules
"""

import importlib
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_registry: dict[str, dict[str, Any]] = {}


def tool(
    name: str,
    description: str,
    schema: dict[str, Any] | None = None,
) -> Callable:
    """Decorator that registers an async function as an ARIA tool."""

    def decorator(fn: Callable) -> Callable:
        if name in _registry:
            logger.warning("Tool '%s' already registered — overwriting", name)
        _registry[name] = {
            "name": name,
            "description": description,
            "schema": schema or {"type": "object", "properties": {}},
            "fn": fn,
        }
        logger.debug("Registered tool: %s", name)
        return fn

    return decorator


def get_all_tools() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the tool registry."""
    return dict(_registry)


def get_tool(name: str) -> dict[str, Any] | None:
    return _registry.get(name)


def get_tools_for_worker(allowed: list[str]) -> dict[str, dict[str, Any]]:
    """Return only the tools in the allowed list — used by WorkerAgent."""
    return {k: v for k, v in _registry.items() if k in allowed}


def build_openai_tools_spec(
    tools: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Convert registry entries to OpenAI function-calling format."""
    source = tools if tools is not None else _registry
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t.get("schema", {"type": "object", "properties": {}}),
            },
        }
        for t in source.values()
    ]


def discover_tools() -> None:
    """Import all tools/*.py and skills/*.py so their @tool decorators run.

    Each module is wrapped in an individual try/except so one broken skill
    never prevents ARIA from starting.
    """
    from core.config import settings  # late import

    _import_dir(Path(__file__).parent.parent / "tools", "tools")
    _import_dir(settings.skills_dir, "skills")


def _import_dir(directory: Path, package_prefix: str) -> None:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"{package_prefix}.{path.stem}"
        try:
            importlib.import_module(module_name)
            logger.debug("Imported %s", module_name)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load %s: %s", module_name, exc)
