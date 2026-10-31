import pytest
from pydantic import ValidationError

from app.core.config import Settings

ENV_EXAMPLE_SECRET = "change-me-use-openssl-rand-hex-32"


def test_the_env_example_jwt_secret_stops_startup_without_echoing_it() -> None:
    with pytest.raises(ValidationError, match="openssl rand -hex 32") as rejected:
        Settings(jwt_secret_key=ENV_EXAMPLE_SECRET)

    assert ENV_EXAMPLE_SECRET not in str(rejected.value)


def test_a_generated_jwt_secret_is_accepted() -> None:
    secret = "3f" * 32

    assert Settings(jwt_secret_key=secret).jwt_secret_key.get_secret_value() == secret
