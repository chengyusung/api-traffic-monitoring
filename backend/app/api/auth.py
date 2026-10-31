from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, Session
from app.schemas.auth import Token, UserCreate, UserRead
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate, session: Session) -> UserRead:
    return UserRead.model_validate(await auth.register(session, data))


@router.post("/token")
async def token(form: Annotated[OAuth2PasswordRequestForm, Depends()], session: Session) -> Token:
    return Token(access_token=await auth.login(session, form.username, form.password))


@router.get("/me")
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
