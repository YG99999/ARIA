"""Shell tools — run_shell_command, write_file, edit_file, read_file, list_directory.

Primary OS-level safety: on Linux/Pi, commands run as the aria-agent restricted user
via preexec_fn. On Windows (dev machine), the preexec_fn is skipped.

_git_snapshot() wraps every write_file and edit_file call for /rollback support.
"""

import asyncio
import logging
import sys
from pathlib import Path

from core.tools import tool
from core.config import settings

logger = logging.getLogger(__name__)

# Secondary denylist — defense-in-depth (primary safety is OS-level user restriction)
_SHELL_DENYLIST = [
    "rm -rf /",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",  # fork bomb
    "chmod 777 /",
]


def _check_denylist(cmd: str) -> str | None:
    lower = cmd.lower()
    for pattern in _SHELL_DENYLIST:
        if pattern in lower:
            return f"Command blocked by denylist: {pattern}"
    return None


async def _git_snapshot(path: str) -> None:
    """Commit a pre-edit snapshot so /rollback can revert file changes."""
    root = str(settings.aria_root)
    proc = await asyncio.create_subprocess_shell(
        f'git -C "{root}" add -A && git -C "{root}" commit -m "pre-edit: {path}" --allow-empty -q 2>/dev/null',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


def _make_preexec():
    """Return a preexec_fn that drops to aria-agent user on Unix."""
    if sys.platform == "win32":
        return None
    try:
        import pwd
        import os
        uid = pwd.getpwnam("aria-agent").pw_uid
        return lambda: os.setuid(uid)
    except (KeyError, ImportError):
        logger.debug("aria-agent user not found — running as current user")
        return None


@tool(
    name="run_shell_command",
    description="Run a shell command and return stdout+stderr. Times out after 120s.",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "number", "description": "Timeout in seconds (default 120)"},
        },
        "required": ["command"],
    },
)
async def run_shell_command(command: str, timeout: float = 120, **kwargs) -> str:
    blocked = _check_denylist(command)
    if blocked:
        return blocked

    preexec = _make_preexec()
    kwargs_proc = {}
    if preexec and sys.platform != "win32":
        kwargs_proc["preexec_fn"] = preexec

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs_proc,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = ""
        if stdout:
            output += stdout.decode(errors="replace")
        if stderr:
            output += "\n[stderr]\n" + stderr.decode(errors="replace")
        rc = f"\n[exit code: {proc.returncode}]"
        return (output + rc)[:8000]
    except asyncio.TimeoutError:
        proc.kill()
        return f"Command timed out after {timeout}s"
    except Exception as exc:
        return f"Error running command: {exc}"


@tool(
    name="write_file",
    description="Write content to a file. Creates parent directories if needed. Git-snapshotted.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "content": {"type": "string", "description": "File content to write"},
        },
        "required": ["path", "content"],
    },
)
async def write_file(path: str, content: str, **kwargs) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    await _git_snapshot(path)
    p.write_text(content, encoding="utf-8")
    return f"Written {len(content)} chars to {path}"


@tool(
    name="edit_file",
    description=(
        "Edit a file using str_replace (old_str → new_str). "
        "Fails if old_str not found or appears more than once. Git-snapshotted."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "old_str": {"type": "string", "description": "Exact string to replace (must be unique in file)"},
            "new_str": {"type": "string", "description": "Replacement string"},
        },
        "required": ["path", "old_str", "new_str"],
    },
)
async def edit_file(path: str, old_str: str, new_str: str, **kwargs) -> str:
    p = Path(path)
    if not p.exists():
        return f"File not found: {path}"

    content = p.read_text(encoding="utf-8")
    count = content.count(old_str)

    if count == 0:
        return f"edit_file failed: old_str not found in {path}"
    if count > 1:
        return f"edit_file failed: old_str appears {count} times in {path} — must be unique"

    await _git_snapshot(path)
    new_content = content.replace(old_str, new_str, 1)
    p.write_text(new_content, encoding="utf-8")
    return f"Replaced 1 occurrence in {path}"


@tool(
    name="read_file",
    description="Read a file's contents. Returns up to 8000 chars.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
        },
        "required": ["path"],
    },
)
async def read_file(path: str, **kwargs) -> str:
    p = Path(path)
    if not p.exists():
        return f"File not found: {path}"
    return p.read_text(encoding="utf-8", errors="replace")[:8000]


@tool(
    name="list_directory",
    description="List files and directories at a path.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path"},
            "recursive": {"type": "boolean", "description": "List recursively (default false)"},
        },
        "required": ["path"],
    },
)
async def list_directory(path: str, recursive: bool = False, **kwargs) -> str:
    p = Path(path)
    if not p.exists():
        return f"Path not found: {path}"
    if not p.is_dir():
        return f"Not a directory: {path}"

    if recursive:
        entries = sorted(p.rglob("*"))
    else:
        entries = sorted(p.iterdir())

    lines = []
    for e in entries[:200]:
        kind = "📁" if e.is_dir() else "📄"
        lines.append(f"{kind} {e.relative_to(p)}")
    return "\n".join(lines) or "(empty)"
