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
    print("  BotFather gives you a token like:  1234567890:ABCdef...")
    print()

    token = ""
    while not token.strip():
        token = _prompt("  Bot token (required)", secret=True)
        if not token.strip():
            print("  Token is required — paste it from BotFather.")

    print()
    print("  Now find your chat ID:")
    print("  1. Send any message to your bot in Telegram")
    print("  2. Visit: https://api.telegram.org/bot" + token[:20] + ".../getUpdates")
    print("  3. Copy the 'id' number from the 'chat' object")
    print()

    chat_id = ""
    while not chat_id.strip().lstrip("-").isdigit():
        chat_id = _prompt("  Your chat_id (numeric, e.g. 123456789)")
        if not chat_id.strip().lstrip("-").isdigit():
            print("  Must be a number.")

    return {"TELEGRAM_TOKEN": token.strip(), "TELEGRAM_CHAT_ID": chat_id.strip()}


def _step_llm() -> tuple[dict[str, str], str]:
    """Returns (env_values, model_id)."""
    print("\n[2/7] LLM API configuration")
    print("  Works with any OpenAI-compatible endpoint:")
    print("    OpenRouter  → https://openrouter.ai/api/v1")
    print("    Anthropic   → https://api.anthropic.com/v1")
    print("    Ollama      → http://localhost:11434/v1")
    print("    NVIDIA, etc → use their OpenAI-compatible URL")
    base_url = _prompt("  API base URL", default="https://openrouter.ai/api/v1")
    api_key = _prompt("  API key", secret=True)
    print()
    print("  Enter the model ID for the main orchestrator.")
    print("  This is the model that runs ARIA's reasoning, planning, and agents.")
    print("  Examples:")
    print("    anthropic/claude-sonnet-4-5   (OpenRouter)")
    print("    anthropic/claude-3-5-sonnet-20241022  (direct Anthropic)")
    print("    gpt-4o                        (OpenAI)")
    print("    llama3.2                      (Ollama)")
    model_id = _prompt("  Orchestrator model ID")
    while not model_id.strip():
        print("  Model ID is required.")
        model_id = _prompt("  Orchestrator model ID")
    return {"LLM_BASE_URL": base_url, "LLM_API_KEY": api_key}, model_id.strip()


async def _test_llm(base_url: str, api_key: str, model_id: str) -> bool:
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        resp = await client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Reply with the single word OK"}],
            max_tokens=10,
        )
        text = resp.choices[0].message.content or ""
        return len(text.strip()) > 0  # any non-empty reply = success
    except Exception as exc:
        print(f"  LLM test failed: {exc}")
        return False


def _write_initial_models_yaml(model_id: str) -> None:
    """Write models.yaml with just the orchestrator model.

    ARIA will ask the user about sub-agent models on first run.
    """
    content = f"""# Model configuration — managed by ARIA
# ARIA asked you about sub-agent models on first startup.
# Edit directly or tell ARIA to update it.

models:
  - id: {model_id}
    tier: heavy
    good_for: "all tasks — orchestrator model"
    compress_at: 100000
    context_limit: 128000

budget:
  daily_usd_alert: 2.00
  daily_usd_hard_stop: 10.00
  monthly_usd_hard_stop: 50.00
"""
    models_path = _ARIA_ROOT / "config" / "models.yaml"
    models_path.write_text(content, encoding="utf-8")
    print(f"  Saved orchestrator model: {model_id}")


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


def _is_wsl() -> bool:
    """Detect Windows Subsystem for Linux."""
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _step_linux_setup() -> None:
    if sys.platform == "win32":
        print("\n[5/7] Linux setup — skipped (Windows detected)")
        return

    if _is_wsl():
        print("\n[5/7] Linux system setup — WSL detected")
        print("  WSL note: aria-agent system user and systemd are not supported in WSL.")
        print("  Start ARIA manually with: aria  (or: python aria/main.py)")
        return

    print("\n[5/7] Linux system setup")

    # Create aria-agent restricted user
    ret = os.system("id aria-agent > /dev/null 2>&1")
    if ret != 0:
        print("  Creating aria-agent system user…")
        os.system("sudo useradd -r -s /bin/bash aria-agent")
    else:
        print("  aria-agent user already exists")

    # Install noVNC dependencies (Pi/Debian)
    install = _prompt("  Install noVNC + x11vnc for remote takeover? (y/n)", default="n")
    if install.lower() == "y":
        print("  Installing packages…")
        os.system("sudo apt-get install -y x11vnc novnc xvfb > /dev/null 2>&1")
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
User={os.environ.get('USER', 'root')}
WorkingDirectory={aria_root}
ExecStart={python_path} {aria_root / 'main.py'}
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    service_path = Path("/etc/systemd/system/aria.service")
    tmp_path = Path(f"/tmp/aria.service")
    tmp_path.write_text(service_content, encoding="utf-8")
    ret = os.system(f"sudo cp {tmp_path} {service_path} && sudo systemctl daemon-reload && sudo systemctl enable aria.service")
    if ret == 0:
        print(f"  Installed {service_path}. Start with: sudo systemctl start aria")
    else:
        print(f"  Could not install systemd service (no sudo?). Start ARIA manually with: aria")


def _step_git_init() -> None:
    if sys.platform == "win32":
        print("\n[6/7] Git init — skipped on Windows")
        return
    if _is_wsl():
        print("\n[6/7] Git init — skipped on WSL (rollback not needed here)")
        return
    print("\n[6/7] Initializing git repository for rollback support")
    aria_root = str(_ARIA_ROOT.resolve())
    if not (_ARIA_ROOT / ".git").exists():
        # Configure a local git identity so commit doesn't fail on bare machines
        os.system(f"git -C {aria_root} init -b main")
        os.system(f'git -C {aria_root} config user.email "aria@localhost"')
        os.system(f'git -C {aria_root} config user.name "ARIA"')
        (_ARIA_ROOT / ".gitignore").write_text(
            ".env\ndata/.secrets_key\ndata/\n__pycache__/\n*.pyc\n",
            encoding="utf-8",
        )
        os.system(f"git -C {aria_root} add -A")
        os.system(f'git -C {aria_root} commit -m "initial" -q')
        print("  Git repository initialized (for /rollback support)")
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
    llm_config, model_id = _step_llm()
    env_values.update(llm_config)

    # Test LLM connectivity with the user's chosen model
    print(f"  Testing connection to {model_id}…", end=" ", flush=True)
    ok = asyncio.run(_test_llm(llm_config["LLM_BASE_URL"], llm_config["LLM_API_KEY"], model_id))
    if ok:
        print("OK ✓")
    else:
        print("FAILED")
        print()
        print("  Common fixes:")
        print("  • Wrong base URL — double-check your provider's OpenAI-compatible endpoint")
        print("  • Wrong API key — copy it fresh from your provider dashboard")
        print("  • Wrong model ID — check exact spelling on your provider's model list")
        print()
        cont = _prompt("  Continue anyway? (y/n)", default="y")
        if cont.lower() == "n":
            print("  Re-run setup with: python aria/main.py --setup")
            sys.exit(0)

    # Write models.yaml with just the orchestrator model.
    # ARIA will ask about sub-agent models on first Telegram startup.
    print("\n[2b/7] Writing model configuration")
    _write_initial_models_yaml(model_id)

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
    print("=" * 60)
    print()
    print("  Starting ARIA now…")
    print("  (Check your Telegram — ARIA will send you a message)")
    print()
    print("  Press Ctrl+C any time to stop.")
    print()

    # Launch ARIA immediately — no need to run a second command
    os.execv(sys.executable, [sys.executable, str(_ARIA_ROOT / "main.py")])


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
