"""Pushover notification service."""
from __future__ import annotations

import json
import logging

import httpx

from app.database import AsyncSessionLocal
from app.models.settings import AppSetting
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

_PUSHOVER_SEND_URL = "https://api.pushover.net/1/messages.json"
_PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"
_SETTINGS_KEY = "pushover_settings"

# Module-level in-memory settings
_app_token: str = ""
_user_key: str = ""
_enabled: bool = False

# Alert toggles
_alert_sync_failure: bool = True
_alert_instance_offline: bool = True
_alert_high_block_rate: bool = False
_alert_no_logs: bool = True

# Thresholds
_block_rate_threshold_pct: float = 50.0
_no_logs_minutes: int = 30


async def _post(app_token: str, user_key: str, message: str, title: str, priority: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _PUSHOVER_SEND_URL,
                data={"token": app_token, "user": user_key, "message": message, "title": title, "priority": priority},
            )
            data = resp.json()
            if data.get("status") == 1:
                return True
            logger.warning("Pushover send failed: %s", data.get("errors", data))
            return False
    except Exception as exc:
        logger.warning("Pushover send error: %s", exc)
        return False


async def send(message: str, title: str = "MyPi", priority: int = 0) -> bool:
    """Send a notification. Respects the enabled flag."""
    if not _enabled or not _app_token or not _user_key:
        return False
    return await _post(_app_token, _user_key, message, title, priority)


async def send_test() -> bool:
    """Send a test notification ignoring the enabled flag (needs credentials only)."""
    if not _app_token or not _user_key:
        return False
    return await _post(_app_token, _user_key, "MyPi test notification — credentials are working!", "MyPi Test", 0)


async def validate(app_token: str, user_key: str) -> tuple[bool, str]:
    """Validate Pushover credentials. Returns (ok, error_message)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _PUSHOVER_VALIDATE_URL,
                data={"token": app_token, "user": user_key},
            )
            data = resp.json()
            if data.get("status") == 1:
                return True, ""
            errors = data.get("errors", ["Unknown error"])
            return False, "; ".join(errors)
    except Exception as exc:
        return False, str(exc)


async def load_settings() -> None:
    """Load Pushover settings from DB. Called at startup."""
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate, _alert_no_logs
    global _block_rate_threshold_pct, _no_logs_minutes

    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _SETTINGS_KEY)

    if not row or not row.value:
        return

    try:
        data = json.loads(row.value)
        _app_token = data.get("app_token", "")
        _user_key = data.get("user_key", "")
        _enabled = data.get("enabled", False)
        _alert_sync_failure = data.get("alert_sync_failure", True)
        _alert_instance_offline = data.get("alert_instance_offline", True)
        _alert_high_block_rate = data.get("alert_high_block_rate", False)
        _alert_no_logs = data.get("alert_no_logs", True)
        _block_rate_threshold_pct = data.get("block_rate_threshold_pct", 50.0)
        _no_logs_minutes = data.get("no_logs_minutes", 30)
        logger.info("Loaded Pushover settings (enabled=%s)", _enabled)
    except Exception as exc:
        logger.warning("Could not parse Pushover settings: %s", exc)


async def save_settings(
    app_token: str,
    user_key: str,
    enabled: bool,
    alert_sync_failure: bool,
    alert_instance_offline: bool,
    alert_high_block_rate: bool,
    alert_no_logs: bool,
    block_rate_threshold_pct: float,
    no_logs_minutes: int,
) -> None:
    """Save Pushover settings to DB and update in-memory state."""
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate, _alert_no_logs
    global _block_rate_threshold_pct, _no_logs_minutes

    _app_token = app_token
    _user_key = user_key
    _enabled = enabled
    _alert_sync_failure = alert_sync_failure
    _alert_instance_offline = alert_instance_offline
    _alert_high_block_rate = alert_high_block_rate
    _alert_no_logs = alert_no_logs
    _block_rate_threshold_pct = block_rate_threshold_pct
    _no_logs_minutes = no_logs_minutes

    payload = json.dumps({
        "app_token": app_token,
        "user_key": user_key,
        "enabled": enabled,
        "alert_sync_failure": alert_sync_failure,
        "alert_instance_offline": alert_instance_offline,
        "alert_high_block_rate": alert_high_block_rate,
        "alert_no_logs": alert_no_logs,
        "block_rate_threshold_pct": block_rate_threshold_pct,
        "no_logs_minutes": no_logs_minutes,
    })

    try:
        async with AsyncSessionLocal() as db:
            stmt = pg_insert(AppSetting).values(key=_SETTINGS_KEY, value=payload)
            stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": payload})
            await db.execute(stmt)
            await db.commit()
        logger.info("Pushover settings persisted to DB.")
    except Exception as exc:
        logger.warning("Could not persist Pushover settings: %s", exc)


def _mask(value: str) -> str:
    """Return last 4 chars of value, or empty string if not set."""
    if not value:
        return ""
    return "****" + value[-4:]


def get_settings() -> dict:
    """Return current settings with masked token/user_key."""
    return {
        "app_token": _mask(_app_token),
        "user_key": _mask(_user_key),
        "enabled": _enabled,
        "alert_sync_failure": _alert_sync_failure,
        "alert_instance_offline": _alert_instance_offline,
        "alert_high_block_rate": _alert_high_block_rate,
        "alert_no_logs": _alert_no_logs,
        "block_rate_threshold_pct": _block_rate_threshold_pct,
        "no_logs_minutes": _no_logs_minutes,
    }


def get_settings_raw() -> dict:
    """Return current settings unmasked."""
    return {
        "app_token": _app_token,
        "user_key": _user_key,
        "enabled": _enabled,
        "alert_sync_failure": _alert_sync_failure,
        "alert_instance_offline": _alert_instance_offline,
        "alert_high_block_rate": _alert_high_block_rate,
        "alert_no_logs": _alert_no_logs,
        "block_rate_threshold_pct": _block_rate_threshold_pct,
        "no_logs_minutes": _no_logs_minutes,
    }


# ── Alert helpers ─────────────────────────────────────────────────────────────

async def notify_sync_failure(error: str) -> None:
    if not _alert_sync_failure:
        return
    await send(f"Sync failed: {error}", title="MyPi Sync Error")


async def notify_instance_offline(name: str) -> None:
    if not _alert_instance_offline:
        return
    await send(f"Instance offline: {name}", title="MyPi Alert")


async def notify_instance_back_online(name: str) -> None:
    if not _alert_instance_offline:
        return
    await send(f"Instance back online: {name}", title="MyPi Alert")
