"""Pi-hole version check service — polls GitHub for the latest Pi-hole releases once per hour."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.settings import AppSetting

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "pihole_version_check"
_GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
_REPOS = {
    "core": "pi-hole/pi-hole",
    "ftl":  "pi-hole/FTL",
    "web":  "pi-hole/web",
}

# In-memory state
_enabled: bool = True
_latest: dict[str, str] = {}
_checked_at: datetime | None = None
_check_in_progress: bool = False


def get_latest_versions() -> dict[str, str]:
    """Return the cached latest versions (empty strings when unknown)."""
    return dict(_latest)


def get_status() -> dict:
    return {
        "enabled": _enabled,
        "latest_core": _latest.get("core", ""),
        "latest_ftl":  _latest.get("ftl", ""),
        "latest_web":  _latest.get("web", ""),
        "checked_at": _checked_at.isoformat() if _checked_at else None,
    }


def compute_update_available(installed: str | None, component: str) -> bool | None:
    """Compare installed version string against cached latest.

    Returns None when the latest version is not yet known so callers can
    distinguish "not checked yet" from "up to date / behind".
    """
    if not installed:
        return None
    latest = _latest.get(component)
    if not latest:
        return None
    return installed != latest


async def load_settings() -> None:
    """Restore enabled flag and cached latest versions from the database on startup."""
    global _enabled, _latest, _checked_at
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, _SETTINGS_KEY)
        if not row or not row.value:
            return
        data = json.loads(row.value)
        _enabled = data.get("enabled", True)
        _latest = {k: data.get(k, "") for k in ("core", "ftl", "web")}
        checked_at_str = data.get("checked_at")
        if checked_at_str:
            _checked_at = datetime.fromisoformat(checked_at_str)
        logger.info(
            "Loaded Pi-hole version cache (enabled=%s, core=%s, ftl=%s, web=%s)",
            _enabled,
            _latest.get("core") or "unknown",
            _latest.get("ftl") or "unknown",
            _latest.get("web") or "unknown",
        )
    except Exception as exc:
        logger.warning("Could not load Pi-hole version cache: %s", exc)


async def save_settings(enabled: bool) -> None:
    global _enabled
    _enabled = enabled
    await _persist()
    logger.info("Pi-hole version check setting saved (enabled=%s)", enabled)


async def _persist() -> None:
    payload = json.dumps({
        "enabled": _enabled,
        "core": _latest.get("core", ""),
        "ftl":  _latest.get("ftl", ""),
        "web":  _latest.get("web", ""),
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
    """Fetch the latest Pi-hole release versions from GitHub and update all instances."""
    global _latest, _checked_at, _check_in_progress
    if not _enabled:
        return
    if _check_in_progress:
        return
    _check_in_progress = True
    try:
        new_latest: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=10, headers={"Accept": "application/vnd.github+json"}) as client:
            for component, repo in _REPOS.items():
                try:
                    resp = await client.get(_GITHUB_API.format(repo=repo))
                    resp.raise_for_status()
                    tag = resp.json().get("tag_name", "")
                    if tag.startswith("v"):
                        tag = tag[1:]
                    new_latest[component] = tag
                except Exception as exc:
                    logger.warning("Pi-hole version check failed for %s: %s", component, exc)
                    # Preserve last known value on transient failure
                    new_latest[component] = _latest.get(component, "")
        _latest = new_latest
        _checked_at = datetime.now(timezone.utc)
        await _persist()
        logger.info(
            "Pi-hole version check complete — core=%s, ftl=%s, web=%s",
            _latest.get("core"), _latest.get("ftl"), _latest.get("web"),
        )
        await _refresh_instance_update_flags()
    except Exception as exc:
        logger.warning("Pi-hole version check error: %s", exc)
    finally:
        _check_in_progress = False


async def _refresh_instance_update_flags() -> None:
    """Recompute update_available_* on every active instance using the cached latest versions."""
    from app.models.pihole import PiholeInstance
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PiholeInstance).where(PiholeInstance.is_active.is_(True))
            )
            instances = list(result.scalars().all())
            for inst in instances:
                if inst.version_core is not None:
                    inst.update_available_core = compute_update_available(inst.version_core, "core")
                if inst.version_ftl is not None:
                    inst.update_available_ftl = compute_update_available(inst.version_ftl, "ftl")
                if inst.version_web is not None:
                    inst.update_available_web = compute_update_available(inst.version_web, "web")
            await db.commit()
    except Exception as exc:
        logger.warning("Could not refresh instance update flags: %s", exc)
