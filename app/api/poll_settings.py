"""Query poll interval settings API."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.models.user import User
from app.services import poll_settings as poll_settings_service

router = APIRouter(prefix="/api/poll-settings", tags=["poll-settings"])


class PollSettingsRequest(BaseModel):
    interval_seconds: int


@router.get("/")
async def get_poll_settings(_: User = Depends(get_current_user)) -> dict:
    return {"interval_seconds": poll_settings_service.get_interval_seconds()}


@router.put("/")
async def save_poll_settings(
    req: PollSettingsRequest,
    _: User = Depends(get_current_user),
) -> dict:
    if req.interval_seconds < 5:
        raise HTTPException(status_code=422, detail="interval_seconds must be at least 5.")
    try:
        await poll_settings_service.save_settings(req.interval_seconds)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save poll settings: {exc}")
    return {"interval_seconds": poll_settings_service.get_interval_seconds()}
