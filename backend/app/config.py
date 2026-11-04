from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo-root .env (backend/app/config.py -> repo root). Missing in containers, where
# compose passes the values as environment variables instead.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    database_url: str


settings = Settings()
