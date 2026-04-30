"""Credential store tools — save/retrieve encrypted credentials and Playwright sessions.

Raw secrets are NEVER injected into LLM prompts. get_credential() output goes
directly into tool calls at the moment of use.
"""

import logging
from pathlib import Path

from core.tools import tool

logger = logging.getLogger(__name__)


@tool(
    name="save_credential",
    description="Save an encrypted credential (password, API key, token) to the secure store.",
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name (e.g. 'github', 'stripe')"},
            "account_label": {"type": "string", "description": "Account label (e.g. 'main', 'work')"},
            "username": {"type": "string", "description": "Username or email (unencrypted, for display)"},
            "kind": {
                "type": "string",
                "enum": ["password", "api_key", "token", "totp_secret"],
                "description": "Credential type",
            },
            "secret": {"type": "string", "description": "The secret value (will be encrypted at rest)"},
        },
        "required": ["service", "account_label", "kind", "secret"],
    },
)
async def save_credential(
    service: str,
    account_label: str,
    kind: str,
    secret: str,
    username: str | None = None,
    **kwargs,
) -> str:
    from core.database import state_db
    from core import secrets

    encrypted = secrets.encrypt(secret)
    await state_db.execute(
        """INSERT INTO credentials(service, account_label, username, kind, secret_enc, source)
           VALUES (?, ?, ?, ?, ?, 'aria')
           ON CONFLICT(id) DO NOTHING""",
        (service, account_label, username, kind, encrypted),
    )
    await state_db.commit()
    return f"Saved credential: {service}/{account_label} ({kind})"


@tool(
    name="get_credential",
    description=(
        "Retrieve a decrypted credential. Use the value directly — never log or display it. "
        "Returns the secret string."
    ),
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name"},
            "account_label": {"type": "string", "description": "Account label"},
        },
        "required": ["service", "account_label"],
    },
)
async def get_credential(service: str, account_label: str, **kwargs) -> str:
    from core.database import state_db
    from core import secrets

    row = await state_db.fetchone(
        "SELECT secret_enc, kind FROM credentials WHERE service=? AND account_label=?",
        (service, account_label),
    )
    if not row:
        return f"No credential found for {service}/{account_label}"

    await state_db.execute(
        "UPDATE credentials SET last_used_at=datetime('now') WHERE service=? AND account_label=?",
        (service, account_label),
    )
    await state_db.commit()

    try:
        return secrets.decrypt(row["secret_enc"])
    except Exception as exc:
        return f"Decryption failed: {exc}"


@tool(
    name="save_session_state",
    description="Save a Playwright browser session for a service account. Enables session-first login.",
    schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name"},
            "account_label": {"type": "string", "description": "Account label"},
            "session_json": {"type": "string", "description": "Playwright storage_state JSON string"},
        },
        "required": ["service", "account_label", "session_json"],
    },
)
async def save_session_state(
    service: str, account_label: str, session_json: str, **kwargs
) -> str:
    from core.config import settings
    from core.database import state_db

    session_dir = settings.data_dir / "browser_state" / service
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / f"{account_label}.json"
    session_path.write_text(session_json, encoding="utf-8")

    await state_db.execute(
        "UPDATE credentials SET session_file=?, last_used_at=datetime('now') "
        "WHERE service=? AND account_label=?",
        (str(session_path), service, account_label),
    )
    await state_db.commit()
    return f"Session saved for {service}/{account_label}"


@tool(
    name="list_accounts",
    description="List all saved accounts (service, label, username, kind). Never shows secrets.",
    schema={
        "type": "object",
        "properties": {},
    },
)
async def list_accounts(**kwargs) -> str:
    from core.database import state_db

    rows = await state_db.fetchall(
        "SELECT service, account_label, username, kind, last_used_at FROM credentials ORDER BY service"
    )
    if not rows:
        return "No accounts saved."
    return "\n".join(
        f"{r['service']}/{r['account_label']} ({r['username'] or '?'}) [{r['kind']}]"
        for r in rows
    )
