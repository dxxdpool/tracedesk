from pathlib import Path
from typing import Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo-root .env (backend/app/config.py -> repo root). Missing in containers, where
# compose passes the values as environment variables instead.
PROJECT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_DIR / ".env"
MIN_TOKEN_LENGTH = 16
PLACEHOLDER_PREFIX = "change-me"


class Settings(BaseSettings):
    # hide_input_in_errors: a rejected token is never echoed into startup logs.
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True)

    database_url: str
    # One bearer token per role (app/auth.py). Missing, short or placeholder tokens stop the app at startup.
    agent_token: SecretStr
    validator_token: SecretStr
    approver_token: SecretStr

    # Clones live in <workspaces_dir>/<investigation_id>/repo. In compose the API sees the folder as
    # /workspaces, so workspaces_host_dir says where agents on the host find the same folder.
    workspaces_dir: Path = PROJECT_DIR / "workspaces"
    workspaces_host_dir: str | None = None
    # Eval/dev only: also accept repos under eval/cases/ instead of GitHub URLs.
    allow_local_repos: bool = False
    local_repos_dir: Path = PROJECT_DIR / "eval" / "cases"
    # Experiment logs: <artifacts_dir>/<investigation_id>/<experiment_id>/stdout.log and stderr.log.
    artifacts_dir: Path = PROJECT_DIR / "artifacts"

    @field_validator("agent_token", "validator_token", "approver_token")
    @classmethod
    def check_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"must be at least {MIN_TOKEN_LENGTH} characters")
        if token.startswith(PLACEHOLDER_PREFIX):
            raise ValueError("is still the .env.example placeholder; generate a real token")
        return value

    @model_validator(mode="after")
    def check_tokens_distinct(self) -> Self:
        tokens = {t.get_secret_value() for t in (self.agent_token, self.validator_token, self.approver_token)}
        if len(tokens) != 3:
            raise ValueError("AGENT_TOKEN, VALIDATOR_TOKEN and APPROVER_TOKEN must all be different")
        return self


settings = Settings()
