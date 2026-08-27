"""Infrastructure settings loaded from `.env` (§1.1, §1.2).

Only secrets and machine-level wiring belong here — things set once at
install time. Everything operational (symbols, thresholds, stage, schedule)
lives in the `config` DB table and is edited from the dashboard; read those
through `app.services.config_service`.
"""

import time
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Captured once, at import time — which happens once per process, at
# startup. Compared against .env's mtime so a stale-running-process can be
# detected and surfaced (see PROCESS_STARTED_AT below and
# routes_system.system_status's env_stale_warning) instead of silently
# looking like a broken integration.
PROCESS_STARTED_AT = time.time()

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    # Secrets
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""
    GEMINI_API_KEY: str = ""

    # Database
    DATABASE_URL: str = (
        "postgresql://postgres:postgres@localhost:5432/trading_pipeline"
    )

    # LLM
    OLLAMA_BASE_URL: str = "http://localhost:11434"


settings = Settings()
