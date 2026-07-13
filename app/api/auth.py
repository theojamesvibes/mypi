import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    _decode_token_claims,
    create_access_token,
    equalize_login_timing,
    generate_api_key,
    get_current_user,
    hash_password,
    is_user_locked_out,
    register_login_failure,
    register_login_success,
    require_mutation,
    verify_password,
    verify_user_password,
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
    # Enforce lockout *before* the password check so a locked account
    # can't be probed for password correctness during the cooldown.
    # Same 401 shape as a wrong password — no enumeration leak — and a
    # dummy bcrypt so the early return isn't a timing oracle either.
    if is_user_locked_out(user):
        equalize_login_timing()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    # verify_user_password runs bcrypt against a dummy hash when user is None
    # so response time doesn't leak whether the username is registered.
    if not verify_user_password(body.password, user):
        await register_login_failure(user, db)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # verify_user_password always returns False for user=None, so the raise
    # above guarantees a real user here. Narrowing only — no runtime change.
    assert user is not None
    await register_login_success(user, db)
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
    authorization: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
):
    # `authorization` must come from the request Header, not from a Cookie of
    # the same name — an earlier version had `Cookie(...)` here, which meant
    # logging out via `Authorization: Bearer …` never actually revoked the
    # JTI. Web UI always uses the session cookie path, so the bug was silent.
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
                datetime.fromtimestamp(exp, tz=UTC)
                if exp
                else datetime.now(UTC)
            )
            stmt = pg_insert(RevokedToken).values(jti=jti, expires_at=expires_at).on_conflict_do_nothing()
            await db.execute(stmt)
            await db.commit()

    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    from app.auth import is_current_request_readonly

    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        is_read_only=is_current_request_readonly(),
    )


@router.post("/api-key", response_model=ApiKeyCreated)
async def create_api_key(
    body: ApiKeyCreate,
    current_user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
):
    raw_key, key_hash = generate_api_key()
    api_key = ApiKey(
        user_id=current_user.id,
        name=body.name,
        key_hash=key_hash,
        key_hash_algo="hmac-sha256",
        is_read_only=body.is_read_only,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    logger.info(
        "user=%s created API key id=%s name=%r read_only=%s",
        current_user.username, api_key.id, api_key.name, api_key.is_read_only,
    )
    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        is_read_only=api_key.is_read_only,
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
    current_user: User = Depends(require_mutation),
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
    logger.info("user=%s revoked API key id=%s name=%r", current_user.username, key.id, key.name)
    return {"detail": "API key revoked"}


# ── Change password ───────────────────────────────────────────────────────────

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


@router.post("/change-password")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=422, detail="Current password is incorrect.")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters.")
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match.")

    user = await db.get(User, current_user.id)
    # current_user was loaded from this same session by the auth dependency,
    # so the primary-key lookup cannot miss. Narrowing only.
    assert user is not None
    user.hashed_password = hash_password(body.new_password)
    user.password_change_required = False
    await db.commit()
    logger.info("user=%s changed their password", current_user.username)

    return {"detail": "Password changed successfully."}


# ── Session timeout ────────────────────────────────────────────────────────────

class SessionTimeoutRequest(BaseModel):
    timeout_minutes: int  # 0 = never


@router.get("/session-timeout")
async def get_session_timeout(_: User = Depends(get_current_user)) -> dict:
    return {"timeout_minutes": session_settings.get_timeout_minutes()}


@router.put("/session-timeout")
async def set_session_timeout(
    body: SessionTimeoutRequest,
    user: User = Depends(require_mutation),
) -> dict:
    if body.timeout_minutes < 0:
        raise HTTPException(status_code=422, detail="timeout_minutes must be >= 0 (0 = never)")
    try:
        await session_settings.save_settings(body.timeout_minutes)
    except Exception as exc:
        logger.exception("Failed to save session timeout: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save session timeout.") from exc
    logger.info("user=%s set session timeout to %d min", user.username, body.timeout_minutes)
    return {"timeout_minutes": session_settings.get_timeout_minutes()}
