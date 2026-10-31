from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import UnauthorizedError
from app.core.security import create_access_token, hash_password, verify_password
from app.db import flush_or_conflict
from app.models import AuditEvent, Role, User
from app.repositories import users
from app.schemas.auth import Email, UserCreate
from app.services import audit

_email = TypeAdapter(Email)


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
        AuditEvent.AUTH_REGISTER,
        "user registered",
        actor_id=user.id,
        target_type="user",
        target_id=user.id,
        context={"email": user.email},
    )
    await session.commit()
    return user


async def login(session: AsyncSession, username: str, password: str) -> str:
    try:
        email: str | None = _email.validate_python(username)
    except ValidationError:
        email = None
    user = await users.get_by_email(session, email) if email else None
    verified = await verify_password(user.password_hash if user else None, password)
    if user is None or not verified or not user.is_active:
        audit.record_event(
            session,
            AuditEvent.AUTH_LOGIN,
            "login failed",
            level="WARNING",
            # Only a well-formed address is stored; anything else is attacker-chosen text.
            context={"email": email or "<invalid-email>", "result": "failure"},
        )
        await session.commit()
        # One message for every cause, so the response never says which part was wrong.
        raise UnauthorizedError("Incorrect email or password.")
    audit.record_event(
        session,
        AuditEvent.AUTH_LOGIN,
        "login succeeded",
        actor_id=user.id,
        context={"email": email, "result": "success"},
    )
    await session.commit()
    return create_access_token(user.id, user.role)
