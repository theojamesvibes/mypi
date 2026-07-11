"""Query poll interval setting — persisted to site_settings under Main.

Phase 4 moved storage from `app_settings` (global) to `site_settings` under
the active Main site. Public API (get_interval_seconds / load_settings /
save_settings / set_reschedule_callback) is unchanged; Phase 5 will add
per-site variants when the sync_service rewrite lands.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

from app.database import AsyncSessionLocal
from app.services.site_settings import get_main_site_id, get_setting, set_setting

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
            main_id = await get_main_site_id(db)
            if main_id is None:
                logger.warning("No Main site found on startup; poll interval deferred.")
                return
            raw = await get_setting(db, main_id, _SETTINGS_KEY)
        if raw:
            data = json.loads(raw)
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
        main_id = await get_main_site_id(db)
        if main_id is None:
            raise RuntimeError(
                "Cannot save poll interval: no Main site found. Run config sync first."
            )
        # site_settings.set_setting commits and verifies with a fresh read.
        await set_setting(db, main_id, _SETTINGS_KEY, value)

    _interval_seconds = interval_seconds
    logger.info("Query poll interval saved: %ds", interval_seconds)

    if _reschedule_fn is not None:
        _reschedule_fn(interval_seconds)
