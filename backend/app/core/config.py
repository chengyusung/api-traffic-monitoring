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

    @field_validator("jwt_secret_key")
    @classmethod
    def reject_placeholder(cls, value: SecretStr) -> SecretStr:
        if "change-me" in value.get_secret_value():
            raise ValueError("replace the .env.example placeholder with `openssl rand -hex 32`")
        return value


settings = Settings()
