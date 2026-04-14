from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import field_validator  # noqa: F401 — kept for database_url validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SESSION_COOKIE_NAME = "session_token"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8  # 8 hours

    initial_admin_user: str = "admin"
    initial_admin_password: str = "changeme"

    # Fernet key for encrypting Pi-hole API passwords at rest.
    # Generate with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""

    # Set to true when MyPi is behind a TLS-terminating reverse proxy (Traefik, nginx, etc.)
    # so session cookies carry the Secure flag.  Leave false for plain-HTTP local access.
    secure_cookies: bool = False

    pihole_config_path: str = "/app/pihole_instances.yml"
    max_pihole_instances: int = 10

    stats_poll_interval: int = 60
    queries_poll_interval: int = 10
    data_retention_days: int = 30

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return v

    def validate_encryption_key_at_startup(self) -> None:
        """Call this during app startup (not at import time) so Alembic migrations
        can run without ENCRYPTION_KEY being set.  Raises RuntimeError if the key
        is missing or invalid, which causes a clean startup failure with a clear message."""
        generate_hint = (
            "python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
        if not self.encryption_key:
            raise RuntimeError(
                "ENCRYPTION_KEY is not set. Add it to your .env file.\n"
                f"Generate one with: {generate_hint}"
            )
        from cryptography.fernet import Fernet
        try:
            Fernet(self.encryption_key.encode())
        except Exception:
            raise RuntimeError(
                "ENCRYPTION_KEY is not a valid Fernet key.\n"
                f"Generate a new one with: {generate_hint}"
            )


class PiholeInstanceConfig:
    def __init__(self, name: str, url: str, password: str, color: str, master: bool = False):
        self.name = name
        self.url = url.rstrip("/")
        self.password = password
        self.color = color
        self.master = master


def load_instance_configs(path: str | None = None) -> list[PiholeInstanceConfig]:
    config_path = path or os.getenv("PIHOLE_CONFIG_PATH", "pihole_instances.yml")
    p = Path(config_path)
    if not p.exists():
        return []
    with open(p) as f:
        data = yaml.safe_load(f)
    if not data or "instances" not in data:
        return []
    instances = []
    raw = data["instances"]
    if len(raw) > settings.max_pihole_instances:
        import logging
        logging.getLogger(__name__).warning(
            "Config has %d instances; only the first %d will be used.",
            len(raw), settings.max_pihole_instances,
        )
    for item in raw[:settings.max_pihole_instances]:
        instances.append(
            PiholeInstanceConfig(
                name=item["name"],
                url=item["url"],
                password=item.get("password", ""),
                color=item.get("color", "#3c8dbc"),
                master=bool(item.get("master", False)),
            )
        )
    return instances


settings = Settings()
