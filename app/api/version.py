"""Version check API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.models.user import User
from app.services import version_check as version_check_service

router = APIRouter(prefix="/api/version", tags=["version"])


class VersionCheckSettingsRequest(BaseModel):
    enabled: bool


@router.get("/status")
async def get_version_status(_: User = Depends(get_current_user)) -> dict:
    return version_check_service.get_status()


@router.put("/settings")
async def save_version_settings(
    req: VersionCheckSettingsRequest,
    _: User = Depends(get_current_user),
) -> dict:
    await version_check_service.save_settings(enabled=req.enabled)
    return version_check_service.get_status()


@router.post("/check")
async def trigger_check(_: User = Depends(get_current_user)) -> dict:
    await version_check_service.check_now()
    return version_check_service.get_status()
