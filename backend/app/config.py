"""Infrastructure settings loaded from `.env` (§1.1, §1.2).

Only secrets and machine-level wiring belong here — things set once at
install time. Everything operational (symbols, thresholds, stage, schedule)
lives in the `config` DB table and is edited from the dashboard; read those
through `app.services.config_service`.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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
