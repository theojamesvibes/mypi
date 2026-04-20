"""Version check API."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user, require_mutation
from app.models.user import User
from app.services import pihole_version_check as pihole_version_check_service
from app.services import version_check as version_check_service

router = APIRouter(prefix="/api/version", tags=["version"])


class VersionCheckSettingsRequest(BaseModel):
    enabled: bool


# ── MyPi version ──────────────────────────────────────────────────────────────

@router.get("/status")
async def get_version_status(_: User = Depends(get_current_user)) -> dict:
    return version_check_service.get_status()


@router.put("/settings")
async def save_version_settings(
    req: VersionCheckSettingsRequest,
    _: User = Depends(require_mutation),
) -> dict:
    await version_check_service.save_settings(enabled=req.enabled)
    return version_check_service.get_status()


@router.post("/check")
async def trigger_check(_: User = Depends(require_mutation)) -> dict:
    await version_check_service.check_now()
    return version_check_service.get_status()


# ── Pi-hole versions ──────────────────────────────────────────────────────────

@router.get("/pihole-status")
async def get_pihole_version_status(_: User = Depends(get_current_user)) -> dict:
    return pihole_version_check_service.get_status()


@router.put("/pihole-settings")
async def save_pihole_version_settings(
    req: VersionCheckSettingsRequest,
    _: User = Depends(require_mutation),
) -> dict:
    await pihole_version_check_service.save_settings(enabled=req.enabled)
    return pihole_version_check_service.get_status()


@router.post("/pihole-check")
async def trigger_pihole_check(_: User = Depends(require_mutation)) -> dict:
    await pihole_version_check_service.check_now()
    return pihole_version_check_service.get_status()
