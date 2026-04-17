"""Query poll interval setting — persisted to app_settings."""
from __future__ import annotations

import json
import logging
from typing import Callable

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.settings import AppSetting

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "queries_poll_interval"
_DEFAULT_INTERVAL = 60  # seconds — used for new installs (no DB row)

_interval_seconds: int = _DEFAULT_INTERVAL
_reschedule_fn: Callable[[int], None] | None = None


def get_interval_seconds() -> int:
    return _interval_seconds


def set_reschedule_callback(fn: Callable[[int], None]) -> None:
    global _reschedule_fn
    _reschedule_fn = fn


async def load_settings() -> None:
    global _interval_seconds
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, _SETTINGS_KEY)
        if row and row.value:
            data = json.loads(row.value)
            _interval_seconds = int(data.get("interval_seconds", _DEFAULT_INTERVAL))
            logger.info("Query poll interval loaded from DB: %ds", _interval_seconds)
        else:
            logger.info("No query poll interval in DB — using default %ds", _DEFAULT_INTERVAL)
    except Exception as exc:
        logger.error("Failed to load query poll interval from DB: %s", exc)


async def save_settings(interval_seconds: int) -> None:
    global _interval_seconds
    value = json.dumps({"interval_seconds": interval_seconds})
    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AppSetting)
            .values(key=_SETTINGS_KEY, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await db.execute(stmt)
        await db.commit()

    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _SETTINGS_KEY)
        if row is None or row.value != value:
            raise RuntimeError(
                f"Query poll interval save verification failed: "
                f"{'nothing found' if row is None else repr(row.value)}"
            )

    _interval_seconds = interval_seconds
    logger.info("Query poll interval saved: %ds", interval_seconds)

    if _reschedule_fn is not None:
        _reschedule_fn(interval_seconds)
