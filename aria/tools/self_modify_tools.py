"""Self-modification tools — edit_persona, update_models, propose_core_change, install_package."""

import asyncio
import logging
from pathlib import Path

from core.tools import tool

logger = logging.getLogger(__name__)


@tool(
    name="edit_persona",
    description="Rewrite ARIA's persona.md (no approval needed — persona is user-facing behavior only).",
    schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "New persona.md content"},
        },
        "required": ["content"],
    },
)
async def edit_persona(content: str, **kwargs) -> str:
    from core.config import settings

    persona_path = settings.aria_root / "config" / "persona.md"
    persona_path.write_text(content, encoding="utf-8")
    return "persona.md updated. Takes effect on next task."


@tool(
    name="update_models",
    description="Rewrite models.yaml and reload model config at next tick.",
    schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "New models.yaml content (valid YAML)"},
        },
        "required": ["content"],
    },
)
async def update_models(content: str, **kwargs) -> str:
    import yaml
    from core.config import settings

    # Validate YAML before writing
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return f"Invalid YAML: {exc}"

    models_path = settings.aria_root / "config" / "models.yaml"
    models_path.write_text(content, encoding="utf-8")
    settings.reload_models()
    return "models.yaml updated and reloaded."


@tool(
    name="propose_core_change",
    description=(
        "Propose a change to a core ARIA source file. "
        "Requires modify_core_code approval. On approve: writes file and runs import self-test."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file (from aria/ root)"},
            "content": {"type": "string", "description": "Full new file content"},
            "reason": {"type": "string", "description": "Why this change is needed"},
        },
        "required": ["path", "content", "reason"],
    },
)
async def propose_core_change(
    path: str,
    content: str,
    reason: str,
    task_id: str | None = None,
    **kwargs,
) -> str:
    from core.approval import ApprovalQueue
    from core.config import settings

    full_path = settings.aria_root / path
    existing = ""
    if full_path.exists():
        existing = full_path.read_text(encoding="utf-8")

    diff_preview = f"File: {path}\nReason: {reason}\n\nNew content ({len(content)} chars):\n{content[:500]}…"

    try:
        await ApprovalQueue.request(
            category="modify_core_code",
            description=f"Modify core file: {path}",
            context_text=diff_preview,
            task_id=task_id,
        )
    except RuntimeError as exc:
        return f"Core change rejected: {exc}"

    # Write file
    full_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = full_path.with_suffix(".py.bak")
    if full_path.exists():
        backup_path.write_text(existing, encoding="utf-8")
    full_path.write_text(content, encoding="utf-8")

    # Runtime import self-test (catches missing deps and logic errors, not just syntax)
    proc = await asyncio.create_subprocess_shell(
        f'python -c "import importlib.util; '
        f'spec=importlib.util.spec_from_file_location(\'m\',r\'{full_path}\'); '
        f'mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        # Restore backup
        if backup_path.exists():
            full_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        err_msg = stderr.decode(errors="replace")[:300]
        return f"Import self-test failed — restored original.\nError: {err_msg}"

    if backup_path.exists():
        backup_path.unlink()
    return f"Core file {path} updated and import-tested successfully."


@tool(
    name="install_package",
    description="Install a Python package via pip and add it to requirements.txt.",
    schema={
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Package name (e.g. 'requests==2.31.0')"},
        },
        "required": ["package"],
    },
)
async def install_package(package: str, **kwargs) -> str:
    import sys

    proc = await asyncio.create_subprocess_shell(
        f"{sys.executable} -m pip install {package} -q",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

    if proc.returncode != 0:
        return f"pip install failed:\n{stderr.decode(errors='replace')[:300]}"

    # Append to requirements.txt
    from core.config import settings
    req_path = settings.aria_root / "requirements.txt"
    if req_path.exists():
        existing = req_path.read_text(encoding="utf-8")
        pkg_base = package.split("==")[0].split(">=")[0].strip()
        if pkg_base.lower() not in existing.lower():
            req_path.write_text(existing.rstrip() + f"\n{package}\n", encoding="utf-8")

    return f"Installed {package} successfully."
