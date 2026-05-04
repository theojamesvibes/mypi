from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import ApiKey, RevokedToken, User

logger = logging.getLogger(__name__)

# How often `last_used_at` on an API key is allowed to be written. An iOS
# client polling once every 30s would otherwise commit a write transaction
# on *every* request just to bump this column. Coalescing to once per
# minute keeps the "when was this key last used" UX accurate without the
# write amplification.
_API_KEY_LAST_USED_COALESCE_SECONDS = 60

# Per-request marker: True when the current principal was authenticated via a
# read-only API key. `require_mutation` raises 403 when this is set. Stored
# in a ContextVar so FastAPI's dependency graph doesn't need to thread an
# extra argument through every handler signature.
_readonly_flag: ContextVar[bool] = ContextVar("mypi_auth_readonly", default=False)


# bcrypt 5.0 raises ValueError on >72-byte input to hashpw; 4.x silently truncated.
# Truncate on both sides so existing stored hashes (made from truncated bytes under
# 4.x) keep verifying and no caller is forced to handle a new exception path.
_BCRYPT_MAX_BYTES = 72

# Pre-computed once at module load. The plaintext is irrelevant — we never
# want it to verify successfully — but checkpw against this hash takes the
# same wall-clock time as a real password check, so login responses don't
# leak whether the submitted username exists. Used by verify_user_password
# below when the user lookup returned None.
_DUMMY_BCRYPT_HASH: str = bcrypt.hashpw(
    b"unused-dummy-for-timing-equalization", bcrypt.gensalt()
).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())


def verify_user_password(plain: str, user: User | None) -> bool:
    """Verify *plain* against *user*'s hash, or against a fixed dummy hash
    when *user* is None. Always pays the cost of one bcrypt verify so the
    response time of a login attempt doesn't reveal whether a username is
    registered. Returns False whenever *user* is None.
    """
    target = user.hashed_password if user is not None else _DUMMY_BCRYPT_HASH
    matched = bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX_BYTES], target.encode())
    return bool(matched and user is not None)


def _jwt_key() -> str:
    """JWT signing key — settings.jwt_secret_key if set, else secret_key."""
    return settings.jwt_secret_key or settings.secret_key


def _api_key_salt() -> str:
    """HMAC key for API-key hashing — settings.api_key_salt if set, else secret_key."""
    return settings.api_key_salt or settings.secret_key


# Allow-list — only HMAC-family algorithms are accepted by both encode and
# decode. Guards against the "ALGORITHM=none in .env" footgun where
# python-jose would happily accept unsigned tokens when "none" is listed.
# RS256/ES256 etc. would require an asymmetric keypair we don't ship, so
# they're not in the list either.
_VALID_JWT_ALGORITHMS: frozenset[str] = frozenset({"HS256", "HS384", "HS512"})


def _jwt_algorithm() -> str:
    """Return the configured algorithm validated to be HMAC-family.

    If `settings.algorithm` is anything outside the allow-list (including
    the "none" footgun), fall back to HS256 and log a warning. Both encode
    and decode call this so the two sides can never diverge.
    """
    if settings.algorithm in _VALID_JWT_ALGORITHMS:
        return settings.algorithm
    logger.warning(
        "ALGORITHM=%r is not in the supported set %s — using HS256.",
        settings.algorithm, sorted(_VALID_JWT_ALGORITHMS),
    )
    return "HS256"


def create_access_token(subject: str, expire_minutes: int | None = None) -> str:
    minutes = expire_minutes if expire_minutes is not None else settings.access_token_expire_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    jti = str(uuid.uuid4())
    return jwt.encode(
        {"sub": subject, "exp": expire, "jti": jti},
        _jwt_key(),
        algorithm=_jwt_algorithm(),
    )


def _decode_token_claims(token: str) -> dict | None:
    """Decode a JWT and return its full claims dict, or None if invalid."""
    try:
        return jwt.decode(token, _jwt_key(), algorithms=[_jwt_algorithm()])
    except JWTError:
        return None


def _decode_token(token: str) -> str | None:
    claims = _decode_token_claims(token)
    return claims.get("sub") if claims else None


def hash_api_key(raw: str) -> str:
    """HMAC-SHA256 keyed on the API-key salt — prevents offline cracking of a DB dump."""
    return hmac.new(_api_key_salt().encode(), raw.encode(), hashlib.sha256).hexdigest()


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
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
) -> User:
    user: User | None = None
    # Determined as we walk auth methods. We set `_readonly_flag` exactly
    # once at the end so a nested resolution from `get_current_user_optional`
    # (when both that dep and `get_current_user` resolve in the same request)
    # cannot stomp the parent call's value.
    is_readonly = False

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
        legacy_upgraded = False

        # Fallback: try legacy plain-SHA256 hash for keys created before 1.4.0
        if api_key is None:
            legacy_hash = _hash_api_key_sha256_legacy(x_api_key)
            result = await db.execute(
                select(ApiKey).where(ApiKey.key_hash == legacy_hash, ApiKey.is_active.is_(True))
            )
            api_key = result.scalar_one_or_none()
            if api_key:
                # Transparently upgrade to HMAC-SHA256 — must commit so the
                # next request finds it under the new hash.
                api_key.key_hash = hmac_hash
                api_key.key_hash_algo = "hmac-sha256"
                legacy_upgraded = True

        if api_key:
            result2 = await db.execute(select(User).where(User.id == api_key.user_id, User.is_active.is_(True)))
            user = result2.scalar_one_or_none()
            if user:
                # Coalesce last_used_at writes — bumping it on every request
                # would commit a write transaction per API call (heavy for
                # iOS clients polling stats). Once-per-minute resolution is
                # plenty for the "last used" UI.
                now = datetime.now(timezone.utc)
                last_used_stale = (
                    api_key.last_used_at is None
                    or (now - api_key.last_used_at).total_seconds()
                    > _API_KEY_LAST_USED_COALESCE_SECONDS
                )
                if last_used_stale:
                    api_key.last_used_at = now
                if legacy_upgraded or last_used_stale:
                    await db.commit()
                if api_key.is_read_only:
                    is_readonly = True
                logger.info('api-key "%s" → %s %s', api_key.name, request.method, request.url.path)

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
    _readonly_flag.set(is_readonly)
    return user


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session_token: str | None = Cookie(default=None),
) -> User | None:
    try:
        return await get_current_user(
            request=request,
            db=db,
            authorization=authorization,
            x_api_key=x_api_key,
            session_token=session_token,
        )
    except HTTPException:
        return None


def require_mutation(_: User = Depends(get_current_user)) -> User:
    """Dependency: accept the call only if the principal can mutate state.

    Read-only API keys fail here with 403. Session cookies and bearer JWTs
    are always allowed — the read-only flag is set exclusively when an
    API key is used.
    """
    if _readonly_flag.get():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key is read-only and cannot perform mutations.",
        )
    return _
