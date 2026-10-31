from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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


settings = Settings()
