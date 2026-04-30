"""ARIA onboarding wizard — run with: python main.py --setup

Guides the user through initial configuration:
  1. Telegram token + chat_id capture
  2. LLM API key + base URL test
  3. Gmail browser login (optional)
  4. User facts collection
  5. noVNC install (Linux/Pi only)
  6. systemd service creation (Linux only)
  7. Beginner/Expert UX mode selection
"""

import os
import sys
import asyncio
from pathlib import Path

_ARIA_ROOT = Path(__file__).parent
_ENV_PATH = _ARIA_ROOT / ".env"


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    if secret:
        import getpass
        return getpass.getpass(f"{label}: ").strip() or default
    value = input(f"{label}{' [' + default + ']' if default else ''}: ").strip()
    return value or default


def _write_env(values: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update(values)
    lines = [f"{k}={v}" for k, v in existing.items()]
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  Saved to {_ENV_PATH}")


def _step_telegram() -> dict[str, str]:
    print("\n[1/7] Telegram configuration")
    print("  Create a bot at https://t.me/BotFather if you haven't already.")
    token = _prompt("  Bot token", secret=True)
    print("  Send any message to your bot, then visit:")
    print("  https://api.telegram.org/bot<TOKEN>/getUpdates")
    chat_id = _prompt("  Your chat_id (numeric)")
    return {"TELEGRAM_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}


def _step_llm() -> dict[str, str]:
    print("\n[2/7] LLM API configuration")
    base_url = _prompt("  API base URL", default="https://openrouter.ai/api/v1")
    api_key = _prompt("  API key", secret=True)
    return {"LLM_BASE_URL": base_url, "LLM_API_KEY": api_key}


async def _test_llm(base_url: str, api_key: str) -> bool:
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        resp = await client.chat.completions.create(
            model="anthropic/claude-haiku-4-5",
            messages=[{"role": "user", "content": "Reply with OK"}],
            max_tokens=5,
        )
        text = resp.choices[0].message.content or ""
        return "ok" in text.lower()
    except Exception as exc:
        print(f"  LLM test failed: {exc}")
        return False


def _step_user_facts() -> dict[str, str]:
    print("\n[3/7] User preferences (stored in memory — press Enter to skip)")
    facts: dict[str, str] = {}
    name = _prompt("  Your name")
    if name:
        facts["user_name"] = name
    location = _prompt("  Your location/timezone")
    if location:
        facts["user_location"] = location
    return facts  # These go into memory DB, not .env


def _step_ux_mode() -> str:
    print("\n[4/7] User experience mode")
    print("  Beginner: Simple status messages, minimal technical details.")
    print("  Expert:   Full task IDs, model usage, agent types, step counts.")
    choice = _prompt("  Are you comfortable with technical details? (y/n)", default="n")
    return "expert" if choice.lower() == "y" else "beginner"


def _step_linux_setup() -> None:
    if sys.platform == "win32":
        print("\n[5/7] Linux setup — skipped (Windows detected)")
        return

    print("\n[5/7] Linux system setup")

    # Create aria-agent restricted user
    ret = os.system("id aria-agent > /dev/null 2>&1")
    if ret != 0:
        print("  Creating aria-agent system user…")
        os.system("useradd -r -s /bin/bash aria-agent")
    else:
        print("  aria-agent user already exists")

    # Install noVNC dependencies (Pi/Debian)
    install = _prompt("  Install noVNC + x11vnc for remote takeover? (y/n)", default="n")
    if install.lower() == "y":
        print("  Installing packages…")
        os.system("apt-get install -y x11vnc novnc xvfb > /dev/null 2>&1")
        print("  Done. Configure x11vnc password separately with: x11vnc -storepasswd")

    # systemd service
    service = _prompt("  Install systemd service for auto-start? (y/n)", default="n")
    if service.lower() == "y":
        _install_systemd_service()


def _install_systemd_service() -> None:
    aria_root = _ARIA_ROOT.resolve()
    python_path = sys.executable
    service_content = f"""[Unit]
Description=ARIA Autonomous AI Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory={aria_root}
ExecStart={python_path} {aria_root / 'main.py'}
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    service_path = Path("/etc/systemd/system/aria.service")
    service_path.write_text(service_content, encoding="utf-8")
    os.system("systemctl daemon-reload")
    os.system("systemctl enable aria.service")
    print(f"  Installed {service_path}. Start with: systemctl start aria")


def _step_git_init() -> None:
    if sys.platform == "win32":
        print("\n[6/7] Git init — skipped on Windows (do this manually if needed)")
        return
    print("\n[6/7] Initializing git repository for rollback support")
    aria_root = str(_ARIA_ROOT.resolve())
    if not (_ARIA_ROOT / ".git").exists():
        os.system(f"git -C {aria_root} init")
        (_ARIA_ROOT / ".gitignore").write_text(
            ".env\ndata/.secrets_key\ndata/\n__pycache__/\n*.pyc\n",
            encoding="utf-8",
        )
        os.system(f"git -C {aria_root} add -A")
        os.system(f'git -C {aria_root} commit -m "initial"')
        print("  Git repository initialized")
    else:
        print("  Git repository already exists")


def run_wizard() -> None:
    print("=" * 60)
    print("  ARIA — Autonomous Resident Intelligence Agent")
    print("  Setup Wizard")
    print("=" * 60)

    env_values: dict[str, str] = {}
    user_facts: dict[str, str] = {}

    env_values.update(_step_telegram())
    llm_config = _step_llm()
    env_values.update(llm_config)

    # Test LLM connectivity
    print("  Testing LLM connectivity…", end=" ", flush=True)
    ok = asyncio.run(_test_llm(llm_config["LLM_BASE_URL"], llm_config["LLM_API_KEY"]))
    print("OK" if ok else "FAILED (check your API key and base URL)")

    user_facts.update(_step_user_facts())
    ux_mode = _step_ux_mode()

    _step_linux_setup()
    _step_git_init()

    print("\n[7/7] Writing configuration")
    _write_env(env_values)

    # Save initial user facts + ux_mode to memory DB (requires DB to be initialized)
    if user_facts or ux_mode:
        print("  Saving user preferences to memory…")
        try:
            _save_initial_facts(user_facts, ux_mode)
        except Exception as exc:
            print(f"  Warning: could not save initial facts: {exc}")

    print("\n" + "=" * 60)
    print("  Setup complete!")
    print("  Start ARIA with: python main.py")
    if sys.platform != "win32":
        print("  Or via systemd:  systemctl start aria")
    print("=" * 60)


def _save_initial_facts(user_facts: dict[str, str], ux_mode: str) -> None:
    from core.config import settings
    from core.database import init_all_databases, memory_db

    settings.load()

    async def _inner() -> None:
        await init_all_databases(
            settings.state_db, settings.memory_db, settings.journal_db
        )
        all_facts = dict(user_facts)
        all_facts["ux_mode"] = ux_mode
        for key, value in all_facts.items():
            # Use safe UPSERT: delete stale FTS row, insert new row, sync FTS
            await memory_db.execute(
                "DELETE FROM facts_fts WHERE rowid = "
                "(SELECT rowid FROM facts WHERE key = ?)",
                (key,),
            )
            await memory_db.execute(
                "INSERT OR REPLACE INTO facts(key, value, source) VALUES (?, ?, 'user')",
                (key, value),
            )
            await memory_db.execute(
                "INSERT INTO facts_fts(rowid, key, value) VALUES "
                "((SELECT rowid FROM facts WHERE key = ?), ?, ?)",
                (key, key, value),
            )
        await memory_db.commit()
        await memory_db.close()

    asyncio.run(_inner())
