"""Pushover notification settings API."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth import get_current_user, require_mutation
from app.limiter import limiter
from app.models.user import User
from app.services import pushover as pushover_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class PushoverSettingsRequest(BaseModel):
    app_token: str = ""
    user_key: str = ""
    enabled: bool = False
    alert_sync_failure: bool = True
    alert_instance_offline: bool = True
    alert_high_block_rate: bool = False
    alert_on_vip_transfer: bool = False
    block_rate_threshold_pct: float = 50.0
    offline_alert_max_count: int = 1
    offline_alert_retries: int = 1


class ValidateRequest(BaseModel):
    app_token: str
    user_key: str


@router.get("/settings")
async def get_settings(_: User = Depends(get_current_user)) -> dict:
    return pushover_service.get_settings()


@router.put("/settings")
async def save_settings(
    req: PushoverSettingsRequest,
    user: User = Depends(require_mutation),
) -> dict:
    existing = pushover_service.get_settings_raw()
    # Only overwrite credentials if the client submitted non-empty values;
    # this lets alert preferences be saved without blanking out saved tokens.
    try:
        await pushover_service.save_settings(
            app_token=req.app_token if req.app_token else existing["app_token"],
            user_key=req.user_key if req.user_key else existing["user_key"],
            enabled=req.enabled,
            alert_sync_failure=req.alert_sync_failure,
            alert_instance_offline=req.alert_instance_offline,
            alert_high_block_rate=req.alert_high_block_rate,
            alert_on_vip_transfer=req.alert_on_vip_transfer,
            block_rate_threshold_pct=req.block_rate_threshold_pct,
            offline_alert_max_count=req.offline_alert_max_count,
            offline_alert_retries=req.offline_alert_retries,
        )
    except Exception as exc:
        logger.exception("Failed to persist Pushover settings: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save notification settings.") from exc
    logger.info("user=%s updated Pushover settings (enabled=%s)", user.username, req.enabled)
    return pushover_service.get_settings()


@router.post("/test")
@limiter.limit("5/minute")
async def send_test(request: Request, user: User = Depends(require_mutation)) -> dict:
    # Rate-limited because each call consumes Pushover's per-month free quota
    # (7500 msg/mo). An unchecked key holder could burn through it quickly.
    logger.info("user=%s sent Pushover test notification", user.username)
    ok = await pushover_service.send_test()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Failed — no credentials saved, or Pushover rejected the request. Check App Token and User Key."
        )
    return {"ok": True}


@router.post("/validate")
@limiter.limit("10/minute")
async def validate_credentials(
    request: Request,
    req: ValidateRequest,
    _: User = Depends(get_current_user),
) -> dict:
    ok, error = await pushover_service.validate(req.app_token, req.user_key)
    return {"ok": ok, "error": error if not ok else None}
