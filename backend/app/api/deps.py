from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import AccessClaims, decode_access_token
from app.db import get_session
from app.models import Role, User
from app.pipeline import Pipeline
from app.repositories import users

# A missing or non-Bearer header raises 401, which the error handler wraps as UNAUTHORIZED.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

Session = Annotated[AsyncSession, Depends(get_session)]


async def authenticate(token: str, session: AsyncSession) -> tuple[User, AccessClaims]:
    """The one token check: REST requests and the WebSocket handshake both call it."""
    claims = decode_access_token(token)
    user = await users.get(session, claims.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid authentication credentials.")
    return user, claims


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], session: Session) -> User:
    user, _ = await authenticate(token, session)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[User], Awaitable[User]]:
    async def check(user: CurrentUser) -> User:
        # user.role comes from the database row, not from the token.
        if user.role not in roles:
            raise ForbiddenError("Your role does not allow this action.")
        return user

    return check


WriterRole = Annotated[User, Depends(require_roles(Role.ADMIN, Role.USER))]
Admin = Annotated[User, Depends(require_roles(Role.ADMIN))]


def get_pipeline(connection: HTTPConnection) -> Pipeline:
    return connection.app.state.pipeline


CurrentPipeline = Annotated[Pipeline, Depends(get_pipeline)]
