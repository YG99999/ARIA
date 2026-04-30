"""execute_python — ARIA's primary self-extension primitive.

Runs arbitrary Python code in a temp file as the aria-agent user (on Linux).
Successful inline code is promoted to a skill automatically by the skill extractor.
"""

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

from core.tools import tool

logger = logging.getLogger(__name__)


@tool(
    name="execute_python",
    description=(
        "Write and execute Python code inline. Use this when no pre-built tool covers the task. "
        "Optionally install packages first. Returns stdout+stderr. Times out after 60s."
    ),
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to execute"},
            "packages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of pip packages to install before running",
            },
            "timeout": {"type": "number", "description": "Timeout in seconds (default 60)"},
        },
        "required": ["code"],
    },
)
async def execute_python(
    code: str,
    packages: list[str] | None = None,
    timeout: float = 60,
    **kwargs,
) -> str:
    # Install packages if requested
    if packages:
        pkg_str = " ".join(packages)
        install_proc = await asyncio.create_subprocess_shell(
            f"{sys.executable} -m pip install {pkg_str} -q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(install_proc.communicate(), timeout=120)
            if install_proc.returncode != 0:
                return f"pip install failed:\n{stderr.decode(errors='replace')[:500]}"
        except asyncio.TimeoutError:
            return "pip install timed out"

    # Write code to temp file and execute
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp_path = f.name

    try:
        from tools.shell import _make_preexec
        preexec = _make_preexec()
        proc_kwargs = {}
        if preexec and sys.platform != "win32":
            proc_kwargs["preexec_fn"] = preexec

        proc = await asyncio.create_subprocess_shell(
            f"{sys.executable} {tmp_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **proc_kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Python execution timed out after {timeout}s"

        output = ""
        if stdout:
            output += stdout.decode(errors="replace")
        if stderr:
            output += "\n[stderr]\n" + stderr.decode(errors="replace")
        output += f"\n[exit code: {proc.returncode}]"
        return output[:8000]

    finally:
        Path(tmp_path).unlink(missing_ok=True)
