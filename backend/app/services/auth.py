from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.db import flush_or_conflict
from app.models import Role, User
from app.repositories import users
from app.schemas.auth import UserCreate, normalize_email
from app.services import audit


async def register(session: AsyncSession, data: UserCreate) -> User:
    user = User(
        email=data.email,
        password_hash=await hash_password(data.password),
        role=Role.USER,
        is_active=True,
    )
    session.add(user)
    await flush_or_conflict(session, "Email is already registered.")
    audit.record_event(
        session,
        "auth.register",
        "user registered",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        context={"email": user.email},
    )
    await session.commit()
    return user


async def login(session: AsyncSession, email: str, password: str) -> str:
    email = normalize_email(email)[:255]
    user = await users.get_by_email(session, email)
    verified = await verify_password(user.password_hash if user else None, password)
    if user is None or not verified or not user.is_active:
        audit.record_event(
            session,
            "auth.login",
            "login failed",
            level="WARNING",
            context={"email": email, "result": "failure"},
        )
        await session.commit()
        # One message for every cause, so the response never says which part was wrong.
        raise UnauthorizedError("Incorrect email or password.")
    audit.record_event(
        session,
        "auth.login",
        "login succeeded",
        actor_id=user.id,
        context={"email": email, "result": "success"},
    )
    await session.commit()
    return create_access_token(user.id, user.role)
