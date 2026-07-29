"""GitHub credential lifecycle with redacted status reporting."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from github import Auth, Github, GithubIntegration

from app.config import settings
from app.exceptions import NotConfiguredError


class GitHubCredentialProvider:
    """Provide PAT or short-lived GitHub App installation credentials."""

    def __init__(self):
        self._lock = threading.Lock()
        self._installation_token = ""
        self._expires_at: datetime | None = None

    @property
    def configured(self) -> bool:
        if settings.GITHUB_AUTH_MODE == "github_app":
            return bool(
                settings.GITHUB_APP_ID
                and settings.GITHUB_APP_INSTALLATION_ID
                and settings.GITHUB_APP_PRIVATE_KEY_FILE
            )
        token = settings.GITHUB_TOKEN.get_secret_value()
        return bool(token and token != "REPLACE_WITH_FINE_GRAINED_GITHUB_TOKEN")

    def _app_auth(self) -> Auth.AppAuth:
        if not self.configured:
            raise NotConfiguredError()
        key = Path(settings.GITHUB_APP_PRIVATE_KEY_FILE or "").read_text(encoding="utf-8")
        return Auth.AppAuth(settings.GITHUB_APP_ID, key)

    def token(self, force_refresh: bool = False) -> str:
        if settings.GITHUB_AUTH_MODE != "github_app":
            token = settings.GITHUB_TOKEN.get_secret_value()
            if not token or token == "REPLACE_WITH_FINE_GRAINED_GITHUB_TOKEN":
                raise NotConfiguredError()
            return token

        with self._lock:
            refresh_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            if (
                not force_refresh
                and self._installation_token
                and self._expires_at
                and self._expires_at > refresh_at
            ):
                return self._installation_token
            integration = GithubIntegration(
                auth=self._app_auth(),
                base_url=settings.GITHUB_API_URL,
            )
            authorization = integration.get_access_token(settings.GITHUB_APP_INSTALLATION_ID)
            self._installation_token = authorization.token
            expires = authorization.expires_at
            self._expires_at = (
                expires.replace(tzinfo=timezone.utc)
                if expires and expires.tzinfo is None
                else expires
            )
            return self._installation_token

    def github(self) -> Github:
        auth = Auth.Token(self.token())
        if settings.GITHUB_API_URL != "https://api.github.com":
            return Github(auth=auth, base_url=settings.GITHUB_API_URL)
        return Github(auth=auth)

    def status(self) -> dict:
        mode = settings.GITHUB_AUTH_MODE
        return {
            "ok": True,
            "configured": self.configured,
            "auth_mode": mode,
            "credential_type": (
                "github_app_installation"
                if mode == "github_app"
                else ("fine_grained_pat" if mode == "fine_grained_pat" else "personal_access_token")
            ),
            "installation_id": settings.GITHUB_APP_INSTALLATION_ID if mode == "github_app" else None,
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
            "cached": bool(self._installation_token) if mode == "github_app" else None,
        }

    def refresh(self) -> dict:
        if settings.GITHUB_AUTH_MODE != "github_app":
            return {
                "ok": False,
                "error": {
                    "code": "GITHUB_APP_NOT_CONFIGURED",
                    "message": "Token refresh is only available in github_app mode",
                },
            }
        self.token(force_refresh=True)
        return self.status()


credential_provider = GitHubCredentialProvider()
