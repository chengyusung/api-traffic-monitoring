from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError
from app.core.security import decode_access_token
from app.db import get_session
from app.models import Role, User
from app.repositories import users

# A missing or non-Bearer header raises 401, which the error handler wraps as UNAUTHORIZED.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")

Session = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], session: Session) -> User:
    user_id = decode_access_token(token)
    try:
        user = await users.get(session, user_id)
    except SQLAlchemyError as exc:
        # Without the users table there is no identity to trust: refuse, never fail open.
        raise ServiceUnavailableError("Database unavailable.") from exc
    if user is None or not user.is_active:
        raise UnauthorizedError("Invalid authentication credentials.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[User], Awaitable[User]]:
    async def check(user: CurrentUser) -> User:
        # user.role comes from the database row, not from the token.
        if user.role not in roles:
            raise ForbiddenError("Your role does not allow this action.")
        return user

    return check


Writer = Annotated[User, Depends(require_roles(Role.ADMIN, Role.USER))]
Admin = Annotated[User, Depends(require_roles(Role.ADMIN))]
