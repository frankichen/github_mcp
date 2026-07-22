import logging
import stat
from pathlib import Path
from typing import Optional

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    GITHUB_AUTH_MODE: str = "legacy"
    GITHUB_TOKEN_FILE: Optional[str] = None
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
    DEPLOY_CALLBACK_API_KEY: SecretStr = SecretStr("")
    DEPLOY_CALLBACK_API_KEY_FILE: Optional[str] = None
    DIAGNOSTIC_MODE: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }

    @model_validator(mode="after")
    def load_github_token_file(self):
        """优先读取受限 Secret 文件，避免 Token 出现在环境变量和进程参数中。"""
        token_file = (self.GITHUB_TOKEN_FILE or "").strip()
        if token_file:
            path = Path(token_file)
            try:
                metadata = path.stat()
            except OSError as exc:
                raise ValueError("GITHUB_TOKEN_FILE is not readable") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("GITHUB_TOKEN_FILE must be a regular file")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("GITHUB_TOKEN_FILE permissions must not be wider than 0600")
            try:
                token = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise ValueError("GITHUB_TOKEN_FILE could not be read") from exc
            if not token:
                raise ValueError("GITHUB_TOKEN_FILE must not be empty")
            if self.GITHUB_TOKEN.get_secret_value():
                logger.warning("GITHUB_TOKEN_FILE is configured; the legacy GITHUB_TOKEN value is ignored")
            self.GITHUB_TOKEN = SecretStr(token)
        elif self.GITHUB_AUTH_MODE == "classic_pat" and not self.GITHUB_TOKEN.get_secret_value():
            raise ValueError("classic_pat authentication requires GITHUB_TOKEN_FILE or GITHUB_TOKEN")
        callback_file = (self.DEPLOY_CALLBACK_API_KEY_FILE or "").strip()
        if callback_file:
            path = Path(callback_file)
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise ValueError("DEPLOY_CALLBACK_API_KEY_FILE must be a mode 0600 regular file")
            self.DEPLOY_CALLBACK_API_KEY = SecretStr(path.read_text(encoding="utf-8").strip())
        return self


settings = Settings()
