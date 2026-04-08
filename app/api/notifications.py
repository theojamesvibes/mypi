"""Pushover notification settings API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.models.user import User
from app.services import pushover as pushover_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class PushoverSettingsRequest(BaseModel):
    app_token: str = ""
    user_key: str = ""
    enabled: bool = False
    alert_sync_failure: bool = True
    alert_instance_offline: bool = True
    alert_high_block_rate: bool = False
    alert_no_logs: bool = True
    block_rate_threshold_pct: float = 50.0
    no_logs_minutes: int = 30


class ValidateRequest(BaseModel):
    app_token: str
    user_key: str


@router.get("/settings")
async def get_settings(_: User = Depends(get_current_user)) -> dict:
    return pushover_service.get_settings()


@router.put("/settings")
async def save_settings(
    req: PushoverSettingsRequest,
    _: User = Depends(get_current_user),
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
            alert_no_logs=req.alert_no_logs,
            block_rate_threshold_pct=req.block_rate_threshold_pct,
            no_logs_minutes=req.no_logs_minutes,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist settings: {exc}")
    return pushover_service.get_settings()


@router.post("/test")
async def send_test(_: User = Depends(get_current_user)) -> dict:
    ok = await pushover_service.send_test()
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Failed — no credentials saved, or Pushover rejected the request. Check App Token and User Key."
        )
    return {"ok": True}


@router.post("/validate")
async def validate_credentials(
    req: ValidateRequest,
    _: User = Depends(get_current_user),
) -> dict:
    ok, error = await pushover_service.validate(req.app_token, req.user_key)
    return {"ok": ok, "error": error if not ok else None}
