from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # A rejected value is often a secret, so errors name the field but never echo the input.
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    database_url: SecretStr
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # RFC 7518 §3.2: an HS256 key needs at least 256 bits.
    jwt_secret_key: SecretStr = Field(min_length=32)
    access_token_expire_minutes: int = Field(default=480, gt=0)

    admin_email: str | None = None
    admin_password: SecretStr | None = None
    seed_demo_data: bool = False
    demo_user_email: str | None = None
    demo_user_password: SecretStr | None = None
    demo_viewer_email: str | None = None
    demo_viewer_password: SecretStr | None = None

    # ADR-001 / ADR-003 parameters.
    simulator_enabled: bool = True
    simulator_seed: int = 42
    simulator_interval_seconds: float = Field(default=1, gt=0)
    buffer_capacity: int = Field(default=1000, gt=0)
    batch_max_size: int = Field(default=100, gt=0)
    flush_interval_seconds: float = Field(default=5, gt=0)
    retry_initial_backoff_seconds: float = Field(default=1, gt=0)
    retry_max_backoff_seconds: float = Field(default=30, gt=0)
    shutdown_flush_timeout_seconds: float = Field(default=10, gt=0)
    commit_timeout_seconds: float = Field(default=10, gt=0)
    threshold_reload_seconds: float = Field(default=5, gt=0)
    ws_max_connections: int = Field(default=100, gt=0)
    ws_per_connection_queue: int = Field(default=100, gt=0)

    @field_validator("jwt_secret_key")
    @classmethod
    def reject_placeholder(cls, value: SecretStr) -> SecretStr:
        if "change-me" in value.get_secret_value():
            raise ValueError("replace the .env.example placeholder with `openssl rand -hex 32`")
        return value


settings = Settings()
