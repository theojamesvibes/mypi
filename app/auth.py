from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import ApiKey, RevokedToken, User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str, expire_minutes: int | None = None) -> str:
    minutes = expire_minutes if expire_minutes is not None else settings.access_token_expire_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    jti = str(uuid.uuid4())
    return jwt.encode(
        {"sub": subject, "exp": expire, "jti": jti},
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def _decode_token_claims(token: str) -> dict | None:
    """Decode a JWT and return its full claims dict, or None if invalid."""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None


def _decode_token(token: str) -> str | None:
    claims = _decode_token_claims(token)
    return claims.get("sub") if claims else None


def hash_api_key(raw: str) -> str:
    """HMAC-SHA256 keyed on SECRET_KEY — prevents offline cracking of a DB dump."""
    return hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _hash_api_key_sha256_legacy(raw: str) -> str:
    """Plain SHA-256 used by keys generated before 1.4.0. Used only for upgrade path."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, hmac_hash). Store only the hash."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


async def _user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username, User.is_active.is_(True)))
    return result.scalar_one_or_none()


async def _is_token_revoked(db: AsyncSession, jti: str) -> bool:
    row = await db.get(RevokedToken, jti)
    return row is not None


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
) -> User:
    user: User | None = None

    # ── Bearer JWT ────────────────────────────────────────────────────────────
    if authorization and authorization.startswith("Bearer "):
        claims = _decode_token_claims(authorization[7:])
        if claims:
            username = claims.get("sub")
            jti = claims.get("jti")
            if username:
                revoked = jti is not None and await _is_token_revoked(db, jti)
                if not revoked:
                    user = await _user_by_username(db, username)

    # ── API key (X-API-Key header) ────────────────────────────────────────────
    if user is None and x_api_key:
        hmac_hash = hash_api_key(x_api_key)
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == hmac_hash, ApiKey.is_active.is_(True))
        )
        api_key = result.scalar_one_or_none()

        # Fallback: try legacy plain-SHA256 hash for keys created before 1.4.0
        if api_key is None:
            legacy_hash = _hash_api_key_sha256_legacy(x_api_key)
            result = await db.execute(
                select(ApiKey).where(ApiKey.key_hash == legacy_hash, ApiKey.is_active.is_(True))
            )
            api_key = result.scalar_one_or_none()
            if api_key:
                # Transparently upgrade to HMAC-SHA256
                api_key.key_hash = hmac_hash
                api_key.key_hash_algo = "hmac-sha256"

        if api_key:
            result2 = await db.execute(select(User).where(User.id == api_key.user_id, User.is_active.is_(True)))
            user = result2.scalar_one_or_none()
            if user:
                api_key.last_used_at = datetime.now(timezone.utc)
                await db.commit()

    # ── Session cookie JWT ────────────────────────────────────────────────────
    if user is None and session_token:
        claims = _decode_token_claims(session_token)
        if claims:
            username = claims.get("sub")
            jti = claims.get("jti")
            if username:
                revoked = jti is not None and await _is_token_revoked(db, jti)
                if not revoked:
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
