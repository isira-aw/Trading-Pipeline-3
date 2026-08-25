import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # System Settings
    APP_SECRET_KEY: str = "default_unsafe_key"
    BINANCE_API_KEY: str = ""
    BINANCE_API_SECRET: str = ""

    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/trading_pipeline"

    # LLM Config
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    GEMINI_API_KEY: str = ""

    class Config:
        env_file = ".env"

settings = Settings()
