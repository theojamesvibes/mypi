from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    _decode_token_claims,
    create_access_token,
    generate_api_key,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import SESSION_COOKIE_NAME, settings
from app.database import get_db
from app.limiter import limiter
from app.models.user import ApiKey, RevokedToken, User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyResponse,
    LoginRequest,
    TokenResponse,
    UserResponse,
)
from app.services import session_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == body.username, User.is_active.is_(True)))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    expire_minutes = session_settings.effective_minutes(session_settings.get_timeout_minutes())
    token = create_access_token(user.username, expire_minutes=expire_minutes)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=expire_minutes * 60,
    )
    return TokenResponse(access_token=token)


@router.post("/logout")
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Cookie(default=None),
    session_token: str | None = Cookie(default=None),
):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    elif session_token:
        token = session_token

    if token:
        claims = _decode_token_claims(token)
        if claims and claims.get("jti"):
            jti = claims["jti"]
            exp = claims.get("exp")
            expires_at = (
                datetime.fromtimestamp(exp, tz=timezone.utc)
                if exp
                else datetime.now(timezone.utc)
            )
            stmt = pg_insert(RevokedToken).values(jti=jti, expires_at=expires_at).on_conflict_do_nothing()
            await db.execute(stmt)
            await db.commit()

    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/api-key", response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw_key, key_hash = generate_api_key()
    api_key = ApiKey(user_id=current_user.id, name=body.name, key_hash=key_hash, key_hash_algo="hmac-sha256")
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        raw_key=raw_key,
    )


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == current_user.id, ApiKey.is_active.is_(True))
    )
    return result.scalars().all()


@router.delete("/api-key/{key_id}")
async def revoke_api_key(
    key_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == current_user.id)
    )
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    await db.commit()
    return {"detail": "API key revoked"}


# ── Session timeout ────────────────────────────────────────────────────────────

class SessionTimeoutRequest(BaseModel):
    timeout_minutes: int  # 0 = never


@router.get("/session-timeout")
async def get_session_timeout(_: User = Depends(get_current_user)) -> dict:
    return {"timeout_minutes": session_settings.get_timeout_minutes()}


@router.put("/session-timeout")
async def set_session_timeout(
    body: SessionTimeoutRequest,
    _: User = Depends(get_current_user),
) -> dict:
    if body.timeout_minutes < 0:
        raise HTTPException(status_code=422, detail="timeout_minutes must be >= 0 (0 = never)")
    try:
        await session_settings.save_settings(body.timeout_minutes)
    except Exception as exc:
        logger.exception("Failed to save session timeout: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save session timeout.")
    return {"timeout_minutes": session_settings.get_timeout_minutes()}
