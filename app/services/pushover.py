"""Pushover notification service."""
from __future__ import annotations

import json
import logging
import uuid

import httpx
from cryptography.fernet import InvalidToken

from app.database import AsyncSessionLocal
from app.models.pihole import _get_fernet
from app.services.site_settings import get_main_site_id, get_setting, set_setting

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

# Module-level in-memory settings. These are the legacy "Main site" config,
# kept in memory for the single-site (site_id=None) code paths. Per-site alerts
# instead read their credentials fresh from the DB via _resolve_site_config();
# most notify_* helpers below branch on that split (in-memory Main vs. per-site).
_app_token: str = ""
_user_key: str = ""
_enabled: bool = False

# Alert toggles
_alert_sync_failure: bool = True
_alert_instance_offline: bool = True
_alert_high_block_rate: bool = False
# VIP cluster transfer alert — fired when the active node in a VIP cluster
# changes (master → replica or back). Defaults off because most operators
# don't need to know the moment failover happens; the affected node will
# still page via the offline / group-stall paths if it actually went down.
_alert_on_vip_transfer: bool = False

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


async def _resolve_site_config(site_id: uuid.UUID) -> dict | None:
    """Return the effective Pushover config for a site (with Main-fallback
    inheritance). None if no credentials configured at this site or Main."""
    try:
        async with AsyncSessionLocal() as db:
            raw = await get_setting(db, site_id, _SETTINGS_KEY)
    except Exception as exc:
        logger.warning("Could not resolve Pushover config for site %s: %s", site_id, exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return {
        "app_token": _decrypt(data.get("app_token", "")),
        "user_key": _decrypt(data.get("user_key", "")),
        "enabled": data.get("enabled", False),
        "alert_sync_failure": data.get("alert_sync_failure", True),
        "alert_instance_offline": data.get("alert_instance_offline", True),
        "alert_high_block_rate": data.get("alert_high_block_rate", False),
        "alert_on_vip_transfer": data.get("alert_on_vip_transfer", False),
        "block_rate_threshold_pct": data.get("block_rate_threshold_pct", 50.0),
        "offline_alert_max_count": data.get("offline_alert_max_count", 1),
        "offline_alert_retries": data.get("offline_alert_retries", 1),
    }


async def send(
    message: str,
    title: str = "MyPi",
    priority: int = 0,
    site_id: uuid.UUID | None = None,
) -> bool:
    """Send a notification using either Main's in-memory config (site_id=None)
    or the target site's config resolved from site_settings (with
    Main-fallback inheritance)."""
    if site_id is None:
        if not _enabled or not _app_token or not _user_key:
            return False
        return await _post(_app_token, _user_key, message, title, priority)
    cfg = await _resolve_site_config(site_id)
    if not cfg or not cfg["enabled"] or not cfg["app_token"] or not cfg["user_key"]:
        return False
    return await _post(cfg["app_token"], cfg["user_key"], message, title, priority)


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
    """Load Pushover settings from DB. Called at startup.

    Reads from `site_settings` under the active Main site. In Phase 4 the
    UI is still Main-only, so this preserves single-site behavior. Phase 5's
    per-site UI rewrite wires in a site_id parameter.
    """
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate
    global _alert_on_vip_transfer
    global _block_rate_threshold_pct, _offline_alert_max_count, _offline_alert_retries

    try:
        async with AsyncSessionLocal() as db:
            main_id = await get_main_site_id(db)
            if main_id is None:
                logger.warning("No Main site found on startup; Pushover settings deferred.")
                return
            raw = await get_setting(db, main_id, _SETTINGS_KEY)
    except Exception as exc:
        logger.warning("Could not query Pushover settings on startup: %s", exc)
        return

    if not raw:
        logger.info("No persisted Pushover settings found in DB — using defaults.")
        return

    try:
        data = json.loads(raw)
        _app_token = _decrypt(data.get("app_token", ""))
        _user_key = _decrypt(data.get("user_key", ""))
        _enabled = data.get("enabled", False)
        _alert_sync_failure = data.get("alert_sync_failure", True)
        _alert_instance_offline = data.get("alert_instance_offline", True)
        _alert_high_block_rate = data.get("alert_high_block_rate", False)
        _alert_on_vip_transfer = data.get("alert_on_vip_transfer", False)
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
    alert_on_vip_transfer: bool = False,
) -> None:
    """Save Pushover settings to DB and update in-memory state."""
    global _app_token, _user_key, _enabled
    global _alert_sync_failure, _alert_instance_offline, _alert_high_block_rate
    global _alert_on_vip_transfer
    global _block_rate_threshold_pct, _offline_alert_max_count, _offline_alert_retries

    payload = json.dumps({
        "app_token": _encrypt(app_token),
        "user_key": _encrypt(user_key),
        "enabled": enabled,
        "alert_sync_failure": alert_sync_failure,
        "alert_instance_offline": alert_instance_offline,
        "alert_high_block_rate": alert_high_block_rate,
        "alert_on_vip_transfer": alert_on_vip_transfer,
        "block_rate_threshold_pct": block_rate_threshold_pct,
        "offline_alert_max_count": offline_alert_max_count,
        "offline_alert_retries": offline_alert_retries,
    })

    async with AsyncSessionLocal() as db:
        main_id = await get_main_site_id(db)
        if main_id is None:
            raise RuntimeError(
                "Cannot save Pushover settings: no Main site found. "
                "Run config sync first."
            )
        # site_settings.set_setting commits and verifies with a fresh read.
        await set_setting(db, main_id, _SETTINGS_KEY, payload)

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
    _alert_on_vip_transfer = alert_on_vip_transfer
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
        "alert_on_vip_transfer": _alert_on_vip_transfer,
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
        "alert_on_vip_transfer": _alert_on_vip_transfer,
        "block_rate_threshold_pct": _block_rate_threshold_pct,
        "offline_alert_max_count": _offline_alert_max_count,
        "offline_alert_retries": _offline_alert_retries,
    }


# ── Alert helpers ─────────────────────────────────────────────────────────────

def _with_site(body: str, site_name: str) -> str:
    """Append a site label to a notification body so multi-site users can tell
    which site fired. No-op when site_name is falsy."""
    return f"{body} ({site_name})" if site_name else body


async def notify_sync_failure(
    error: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    """When site_id is set, the per-site alert toggle + credentials are used
    (with Main-fallback). Otherwise the legacy Main-only config is used."""
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_sync_failure"]:
            return
    elif not _alert_sync_failure:
        return
    await send(
        _with_site(f"Sync failed: {error}", site_name),
        title="MyPi Sync Error",
        site_id=site_id,
    )


async def notify_instance_offline(
    name: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site(f"Instance offline: {name}", site_name),
        title="MyPi Alert",
        site_id=site_id,
    )


async def notify_instance_back_online(
    name: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site(f"Instance back online: {name}", site_name),
        title="MyPi Alert",
        site_id=site_id,
    )


async def notify_instance_stalled(
    name: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    """Pi-hole's admin API still answers but the query log has stopped
    advancing — typical aftermath of a Pi-hole upgrade leaving FTL half-up.
    Reuses the offline alert toggle so users who've muted offline alerts
    don't get woken up twice; a dedicated stalled-alert setting can come
    later if the two need to diverge.
    """
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site(
            f"Instance stalled: {name} — admin API responsive but query "
            f"logging frozen. Try `systemctl restart pihole-FTL` on the host.",
            site_name,
        ),
        title="MyPi Alert",
        site_id=site_id,
    )


async def notify_instance_recovered_from_stall(
    name: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site(f"Instance recovered: {name} — query logging resumed.", site_name),
        title="MyPi Alert",
        site_id=site_id,
    )


async def notify_vip_transfer(
    old_name: str,
    new_name: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    """The active node in a VIP cluster shifted from `old_name` to
    `new_name`. Gated by the dedicated `alert_on_vip_transfer` toggle —
    independent of the offline / sync-failure paths because most operators
    don't need to know about every failover."""
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg.get("alert_on_vip_transfer"):
            return
    elif not _alert_on_vip_transfer:
        return
    await send(
        _with_site(
            f"VIP transfer: now serving from {new_name} (was {old_name}).",
            site_name,
        ),
        title="MyPi VIP Transfer",
        site_id=site_id,
    )


async def notify_vip_group_stalled(
    names: str,
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    """Every node in a VIP cluster has gone flat — the whole VIP appears
    dead. Reuses the offline alert toggle: if you've muted offline alerts
    you don't get woken up here either."""
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site(
            f"VIP cluster stalled — every node ({names}) has stopped "
            f"serving DNS. The whole VIP appears dead.",
            site_name,
        ),
        title="MyPi VIP Down",
        site_id=site_id,
    )


async def notify_vip_group_recovered(
    site_name: str = "",
    site_id: uuid.UUID | None = None,
) -> None:
    if site_id is not None:
        cfg = await _resolve_site_config(site_id)
        if cfg is None or not cfg["alert_instance_offline"]:
            return
    elif not _alert_instance_offline:
        return
    await send(
        _with_site("VIP cluster recovered — at least one node is serving DNS again.", site_name),
        title="MyPi VIP",
        site_id=site_id,
    )
