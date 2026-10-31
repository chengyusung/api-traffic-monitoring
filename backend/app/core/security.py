import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings
from app.core.errors import TokenExpiredError, UnauthorizedError
from app.models import Role

ALGORITHM = "HS256"
_hasher = PasswordHasher()  # argon2-cffi defaults to Argon2id
# Verified when the email is unknown, so a miss costs the same time as a wrong password.
_DUMMY_HASH = _hasher.hash(uuid.uuid4().hex)
# Each call holds 64 MiB and about four cores for ~25 ms; a login burst waits here, not in RAM.
_argon2_slots = asyncio.Semaphore(2)


async def hash_password(password: str) -> str:
    async with _argon2_slots:
        return await asyncio.to_thread(_hasher.hash, password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str | None, password: str) -> bool:
    async with _argon2_slots:
        matched = await asyncio.to_thread(_verify, password_hash or _DUMMY_HASH, password)
    return matched and password_hash is not None


def create_access_token(user_id: int, role: Role, now: datetime | None = None) -> str:
    issued_at = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_expire_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret_key.get_secret_value(), algorithm=ALGORITHM)


class AccessClaims(NamedTuple):
    user_id: int
    expires_at: datetime


def decode_access_token(token: str) -> AccessClaims:
    """Verify the token. The role claim is ignored: callers reload the user."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "role", "exp", "iat", "jti"]},
        )
        return AccessClaims(int(claims["sub"]), datetime.fromtimestamp(claims["exp"], UTC))
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise UnauthorizedError("Invalid authentication credentials.") from exc
