from pydantic_settings import BaseSettings
from pydantic import SecretStr
from typing import Optional


class Settings(BaseSettings):
    GITHUB_TOKEN: SecretStr = SecretStr("")
    ACTION_API_KEY: SecretStr = SecretStr("")
    GITHUB_API_URL: str = "https://api.github.com"
    ALLOWED_REPOSITORIES: str = "*"
    ALLOW_DEFAULT_BRANCH_WRITE: bool = False
    MAX_FILE_CHARACTERS: int = 60000
    MAX_TOTAL_CHARACTERS: int = 80000
    MAX_FILES_PER_COMMIT: int = 20
    LOG_LEVEL: str = "INFO"
    IDEMPOTENCY_DB_PATH: str = "/data/idempotency.db"
    IDEMPOTENCY_TTL_HOURS: int = 24
    SERVICE_URL: str = "https://github.555044.xyz"
    DIAGNOSTIC_MODE: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
