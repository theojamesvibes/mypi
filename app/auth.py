from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import ApiKey, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": subject, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def _decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        return payload.get("sub")
    except JWTError:
        return None


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, hash). Store only the hash."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


async def _user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username, User.is_active.is_(True)))
    return result.scalar_one_or_none()


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
) -> User:
    user: User | None = None

    if authorization and authorization.startswith("Bearer "):
        username = _decode_token(authorization[7:])
        if username:
            user = await _user_by_username(db, username)

    if user is None and x_api_key:
        key_hash = hash_api_key(x_api_key)
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
        )
        api_key = result.scalar_one_or_none()
        if api_key:
            result2 = await db.execute(select(User).where(User.id == api_key.user_id, User.is_active.is_(True)))
            user = result2.scalar_one_or_none()
            if user:
                api_key.last_used_at = datetime.now(timezone.utc)
                await db.commit()

    if user is None and session_token:
        username = _decode_token(session_token)
        if username:
            user = await _user_by_username(db, username)

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def get_current_user_optional(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
) -> User | None:
    try:
        return await get_current_user(db=db, authorization=authorization, x_api_key=x_api_key, session_token=session_token)
    except HTTPException:
        return None
