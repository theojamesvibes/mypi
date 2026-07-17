"""Version check service — polls GitHub for the latest MyPi release once per hour."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.settings import AppSetting

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "version_check"
_VERSION_URL = "https://raw.githubusercontent.com/theojamesvibes/mypi/main/VERSION"
_RELEASE_URL = "https://github.com/theojamesvibes/mypi/blob/main/CHANGELOG.md"

# In-memory state
_enabled: bool = True
_current_version: str = ""
_latest_version: str = ""
_checked_at: datetime | None = None
_check_in_progress: bool = False


def initialize(current_version: str) -> None:
    """Call at startup to register the running version."""
    global _current_version
    _current_version = current_version


def _parse_version(v: str) -> tuple[int, ...]:
    """Convert a dotted version string ("2.10.1") to a tuple of numbers
    ((2, 10, 1)) so versions compare numerically — otherwise "2.10" would
    sort before "2.9" as plain text. Unparseable input becomes (0,), i.e.
    treated as the oldest possible, so we never falsely claim we're current.
    """
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0,)


def get_status() -> dict:
    """Report the running vs latest MyPi version for the settings page.

    `up_to_date` is True/False once we've checked GitHub, or None when no
    check has succeeded yet (so the UI can distinguish "unknown" from "behind").
    """
    if _latest_version:
        up_to_date = _parse_version(_current_version) >= _parse_version(_latest_version)
    else:
        up_to_date = None
    return {
        "enabled": _enabled,
        "current_version": _current_version,
        "latest_version": _latest_version,
        "up_to_date": up_to_date,
        "checked_at": _checked_at.isoformat() if _checked_at else None,
        "release_url": _RELEASE_URL,
    }


async def load_settings() -> None:
    """Restore the in-memory state (enabled flag, last-known latest version,
    last check time) from the app_settings row on startup so it survives
    container restarts."""
    global _enabled, _latest_version, _checked_at
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, _SETTINGS_KEY)
        if not row or not row.value:
            return
        data = json.loads(row.value)
        _enabled = data.get("enabled", True)
        _latest_version = data.get("latest_version", "")
        checked_at_str = data.get("checked_at")
        if checked_at_str:
            _checked_at = datetime.fromisoformat(checked_at_str)
        logger.info(
            "Loaded version check settings (enabled=%s, latest=%s)",
            _enabled,
            _latest_version or "unknown",
        )
    except Exception as exc:
        logger.warning("Could not load version check settings: %s", exc)


async def save_settings(enabled: bool) -> None:
    """Turn the hourly version check on/off and persist the choice."""
    global _enabled
    _enabled = enabled
    await _persist()
    logger.info("Version check setting saved (enabled=%s)", enabled)


async def _persist() -> None:
    """Mirror the current in-memory state to the app_settings row (upsert)."""
    payload = json.dumps({
        "enabled": _enabled,
        "latest_version": _latest_version,
        "checked_at": _checked_at.isoformat() if _checked_at else None,
    })
    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AppSetting)
            .values(key=_SETTINGS_KEY, value=payload)
            .on_conflict_do_update(index_elements=["key"], set_={"value": payload})
        )
        await db.execute(stmt)
        await db.commit()


async def check_now() -> None:
    """Fetch the latest version from GitHub and store the result."""
    global _latest_version, _checked_at, _check_in_progress
    if not _enabled:
        return
    if _check_in_progress:
        return
    _check_in_progress = True
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_VERSION_URL)
            resp.raise_for_status()
            latest = resp.text.strip()
        _latest_version = latest
        _checked_at = datetime.now(UTC)
        await _persist()
        logger.info("Version check complete — latest: %s (running: %s)", latest, _current_version)
    except Exception as exc:
        logger.warning("Version check failed: %s", exc)
    finally:
        _check_in_progress = False
