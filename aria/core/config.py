"""Settings singleton — loads .env and models.yaml, exposes typed config accessors."""

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Resolve paths relative to aria/ root
_ARIA_ROOT = Path(__file__).parent.parent.resolve()
_CONFIG_DIR = _ARIA_ROOT / "config"
_DATA_DIR_DEFAULT = _ARIA_ROOT / "data"
_SKILLS_DIR_DEFAULT = _ARIA_ROOT / "skills"


class Settings:
    _instance: "Settings | None" = None

    def __new__(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def load(self, env_path: Path | None = None) -> None:
        if self._loaded:
            return

        # Load .env — look next to aria/ root by default
        dot_env = env_path or (_ARIA_ROOT / ".env")
        if dot_env.exists():
            load_dotenv(dot_env)

        # Telegram
        self.telegram_token: str = os.environ["TELEGRAM_TOKEN"]
        self.telegram_chat_id: int = int(os.environ["TELEGRAM_CHAT_ID"])

        # LLM
        self.llm_api_key: str = os.environ["LLM_API_KEY"]
        self.llm_base_url: str = os.environ.get(
            "LLM_BASE_URL", "https://openrouter.ai/api/v1"
        )

        # Paths
        self.aria_root: Path = _ARIA_ROOT
        self.data_dir: Path = Path(os.environ.get("DATA_DIR", str(_DATA_DIR_DEFAULT)))
        self.skills_dir: Path = Path(
            os.environ.get("SKILLS_DIR", str(_SKILLS_DIR_DEFAULT))
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        # Database paths
        self.state_db: Path = self.data_dir / "state.db"
        self.memory_db: Path = self.data_dir / "memory.db"
        self.journal_db: Path = self.data_dir / "journal.db"

        # Browser
        self.browser_headless: bool = os.environ.get(
            "BROWSER_HEADLESS", "true" if sys.platform != "win32" else "false"
        ).lower() == "true"
        self.browser_profile_dir: Path = self.data_dir / "browser_profiles" / "default"
        self.browser_profile_dir.mkdir(parents=True, exist_ok=True)

        # Optional integrations
        self.capsolver_key: str | None = os.environ.get("CAPSOLVER_KEY")
        self.tailscale_ip: str | None = os.environ.get("TAILSCALE_IP")
        self.semantic_memory: bool = (
            os.environ.get("SEMANTIC_MEMORY", "false").lower() == "true"
        )

        # Webhook secrets — stored as {source: secret} dict
        self.webhook_secrets: dict[str, str] = {}
        for key, val in os.environ.items():
            if key.startswith("WEBHOOK_SECRET_"):
                source = key[len("WEBHOOK_SECRET_"):].lower()
                self.webhook_secrets[source] = val

        # Models + budget
        self._models_yaml = self._load_yaml("models.yaml")
        self._agent_modes_yaml = self._load_yaml("agent_modes.yaml")

        self._loaded = True

    # ------------------------------------------------------------------
    # Model helpers
    # ------------------------------------------------------------------

    def get_model(self, tier: str) -> str:
        """Return model id for the given tier ('light' or 'heavy').

        Falls back to the first model if only one is configured.
        """
        models = self._models_yaml.get("models", [])
        if not models:
            raise RuntimeError("No models configured in models.yaml")
        for m in models:
            if m.get("tier") == tier:
                return m["id"]
        # Single model fallback
        return models[0]["id"]

    def get_model_config(self, model_id: str) -> dict[str, Any]:
        models = self._models_yaml.get("models", [])
        for m in models:
            if m["id"] == model_id:
                return m
        return {}

    @property
    def budget(self) -> dict[str, float]:
        return self._models_yaml.get("budget", {
            "daily_usd_alert": 2.0,
            "daily_usd_hard_stop": 10.0,
            "monthly_usd_hard_stop": 50.0,
        })

    @property
    def agent_modes(self) -> dict[str, Any]:
        return self._agent_modes_yaml

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_yaml(self, filename: str) -> dict[str, Any]:
        path = _CONFIG_DIR / filename
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def reload_models(self) -> None:
        """Reload models.yaml at runtime (called after update_models tool)."""
        self._models_yaml = self._load_yaml("models.yaml")


# Module-level singleton — import and call settings.load() at startup
settings = Settings()
