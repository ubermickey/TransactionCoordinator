"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration is loaded from .env or environment variables."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Claude API
    anthropic_api_key: str = ""

    # Google Workspace
    google_credentials_file: str = "credentials.json"
    google_token_file: str = "token.json"
    gmail_send_as_email: str = ""
    agent_email: str = ""

    # DocuSign
    docusign_integration_key: str = ""
    docusign_secret_key: str = ""
    docusign_account_id: str = ""
    docusign_base_url: str = "https://demo.docusign.net/restapi"
    docusign_oauth_base: str = "https://account-d.docusign.com"

    # Push Notifications
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Application
    data_dir: str = "./data"
    timezone: str = "America/Los_Angeles"
    daily_digest_time: str = "08:00"
    jurisdictions_dir: str = "./jurisdictions"
    workflow_dir: str = "./workflow"

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def jurisdictions_path(self) -> Path:
        return Path(self.jurisdictions_dir)

    @property
    def workflow_path(self) -> Path:
        return Path(self.workflow_dir)

    def has_pushover(self) -> bool:
        return bool(self.pushover_user_key and self.pushover_api_token)

    def has_ntfy(self) -> bool:
        return bool(self.ntfy_topic)

    def has_docusign(self) -> bool:
        return bool(self.docusign_integration_key and self.docusign_account_id)

    def has_google(self) -> bool:
        return Path(self.google_credentials_file).exists()

    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)


def get_settings() -> Settings:
    return Settings()
