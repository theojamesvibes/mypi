"""Session timeout setting — persisted to app_settings."""
from __future__ import annotations

import json
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.settings import AppSetting

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "session_timeout"
_DEFAULT_TIMEOUT_MINUTES = 480   # 8 hours
_NEVER_TIMEOUT_MINUTES = 5_256_000  # ~10 years

_timeout_minutes: int = _DEFAULT_TIMEOUT_MINUTES


def get_timeout_minutes() -> int:
    return _timeout_minutes


def effective_minutes(stored: int) -> int:
    """Convert stored value to actual JWT expiry minutes. 0 = never → ~10 years."""
    return _NEVER_TIMEOUT_MINUTES if stored == 0 else stored


async def load_settings() -> None:
    global _timeout_minutes
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, _SETTINGS_KEY)
        if row and row.value:
            data = json.loads(row.value)
            _timeout_minutes = int(data.get("timeout_minutes", _DEFAULT_TIMEOUT_MINUTES))
            logger.info("Session timeout loaded from DB: %d minutes (0=never)", _timeout_minutes)
        else:
            logger.info("No session timeout in DB — using default %d minutes", _DEFAULT_TIMEOUT_MINUTES)
    except Exception as exc:
        logger.error("Failed to load session timeout from DB: %s", exc)


async def save_settings(timeout_minutes: int) -> None:
    global _timeout_minutes
    value = json.dumps({"timeout_minutes": timeout_minutes})
    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AppSetting)
            .values(key=_SETTINGS_KEY, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await db.execute(stmt)
        await db.commit()

    # Verify in a fresh session
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _SETTINGS_KEY)
        if row is None or row.value != value:
            raise RuntimeError(
                f"Session timeout save verification failed: "
                f"{'nothing found' if row is None else repr(row.value)}"
            )

    _timeout_minutes = timeout_minutes
    logger.info("Session timeout saved: %d minutes (0=never)", timeout_minutes)
