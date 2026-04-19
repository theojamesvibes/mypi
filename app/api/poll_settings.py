"""Query poll interval settings API."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user, require_mutation
from app.models.user import User
from app.services import poll_settings as poll_settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/poll-settings", tags=["poll-settings"])


class PollSettingsRequest(BaseModel):
    interval_seconds: int


@router.get("/")
async def get_poll_settings(_: User = Depends(get_current_user)) -> dict:
    return {"interval_seconds": poll_settings_service.get_interval_seconds()}


@router.put("/")
async def save_poll_settings(
    req: PollSettingsRequest,
    user: User = Depends(require_mutation),
) -> dict:
    if req.interval_seconds < 5:
        raise HTTPException(status_code=422, detail="interval_seconds must be at least 5.")
    try:
        await poll_settings_service.save_settings(req.interval_seconds)
    except Exception as exc:
        # Don't echo the underlying exception — it can leak DB schema /
        # column names / SQL fragments to a caller. Log it for the operator
        # and return a generic 500 to the client.
        logger.exception("Failed to save poll settings: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save poll settings.")
    logger.info("user=%s set query poll interval to %ds", user.username, req.interval_seconds)
    return {"interval_seconds": poll_settings_service.get_interval_seconds()}
