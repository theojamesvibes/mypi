"""Pushover notification service."""
from __future__ import annotations

import json
import logging

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.pihole import _get_fernet
from app.models.settings import AppSetting

logger = logging.getLogger(__name__)


def _encrypt(value: str) -> str:
    """Fernet-encrypt a credential string. Empty values pass through."""
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    """Decrypt a Fernet token. Treats invalid tokens as legacy plaintext.

    Settings written before this commit landed are stored as plain text; we
    return them as-is so the next save_settings call re-encrypts them.
    """
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return value

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

# Thresholds
_block_rate_threshold_pct: float = 50.0

# Offline alert repeat: 0 = always (every poll), 1-10 = max alerts per outage period
_offline_alert_max_count: int = 1

# Retries before the first offline alert fires (1-10; each retry = 1 poll = 60 s)
_offline_alert_retries: int = 1


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


def get_offline_alert_max_count() -> int:
    """Return the max number of offline alerts per outage period (0 = always)."""
    return _offline_alert_max_count


def get_offline_alert_retries() -> int:
    """Return the number of consecutive offline polls to tolerate before alerting."""
    return _offline_alert_retries


async def load_settings() -> None:
    """Load Pushover settings from DB. Called at startup."""
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate
    global _block_rate_threshold_pct, _offline_alert_max_count, _offline_alert_retries

    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, _SETTINGS_KEY)
    except Exception as exc:
        logger.warning("Could not query Pushover settings on startup: %s", exc)
        return

    if not row or not row.value:
        logger.info("No persisted Pushover settings found in DB — using defaults.")
        return

    try:
        data = json.loads(row.value)
        _app_token = _decrypt(data.get("app_token", ""))
        _user_key = _decrypt(data.get("user_key", ""))
        _enabled = data.get("enabled", False)
        _alert_sync_failure = data.get("alert_sync_failure", True)
        _alert_instance_offline = data.get("alert_instance_offline", True)
        _alert_high_block_rate = data.get("alert_high_block_rate", False)
        _block_rate_threshold_pct = data.get("block_rate_threshold_pct", 50.0)
        _offline_alert_max_count = data.get("offline_alert_max_count", 1)
        _offline_alert_retries = data.get("offline_alert_retries", 1)
        # Legacy keys alert_no_logs / no_logs_minutes may be present in older
        # rows (1.8.x wired them in the UI but the notification was never
        # implemented).  Silently ignored here — on the next save they drop
        # out of the persisted JSON.
        logger.info("Loaded Pushover settings from DB (enabled=%s, token_set=%s)", _enabled, bool(_app_token))
    except Exception as exc:
        logger.warning("Could not parse Pushover settings: %s", exc)


async def save_settings(
    app_token: str,
    user_key: str,
    enabled: bool,
    alert_sync_failure: bool,
    alert_instance_offline: bool,
    alert_high_block_rate: bool,
    block_rate_threshold_pct: float,
    offline_alert_max_count: int = 1,
    offline_alert_retries: int = 1,
) -> None:
    """Save Pushover settings to DB and update in-memory state."""
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate
    global _block_rate_threshold_pct, _offline_alert_max_count, _offline_alert_retries

    payload = json.dumps({
        "app_token": _encrypt(app_token),
        "user_key": _encrypt(user_key),
        "enabled": enabled,
        "alert_sync_failure": alert_sync_failure,
        "alert_instance_offline": alert_instance_offline,
        "alert_high_block_rate": alert_high_block_rate,
        "block_rate_threshold_pct": block_rate_threshold_pct,
        "offline_alert_max_count": offline_alert_max_count,
        "offline_alert_retries": offline_alert_retries,
    })

    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AppSetting)
            .values(key=_SETTINGS_KEY, value=payload)
            .on_conflict_do_update(index_elements=["key"], set_={"value": payload})
        )
        await db.execute(stmt)
        await db.commit()

    # Verify the write landed in a fresh session
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _SETTINGS_KEY)
        if row is None or row.value != payload:
            raise RuntimeError(
                f"DB write verification failed for Pushover settings: "
                f"committed but read-back returned {'nothing' if row is None else 'wrong value'}"
            )

    # Update in-memory state only after the DB write has been verified.
    # If verification raises, the next load_settings() restores the truth
    # from disk and we don't end up with in-memory state diverging from
    # what's persisted.
    _app_token = app_token
    _user_key = user_key
    _enabled = enabled
    _alert_sync_failure = alert_sync_failure
    _alert_instance_offline = alert_instance_offline
    _alert_high_block_rate = alert_high_block_rate
    _block_rate_threshold_pct = block_rate_threshold_pct
    _offline_alert_max_count = offline_alert_max_count
    _offline_alert_retries = offline_alert_retries

    logger.info("Pushover settings persisted and verified in DB.")


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
        "block_rate_threshold_pct": _block_rate_threshold_pct,
        "offline_alert_max_count": _offline_alert_max_count,
        "offline_alert_retries": _offline_alert_retries,
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
        "block_rate_threshold_pct": _block_rate_threshold_pct,
        "offline_alert_max_count": _offline_alert_max_count,
        "offline_alert_retries": _offline_alert_retries,
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
