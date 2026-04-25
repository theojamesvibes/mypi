"""Display-preferences API.

A single GET/PUT pair for user-facing display toggles that don't fit the
notification or instance-config endpoints. Stored under the active Main
site in `site_settings`; reads from any site fall back to Main via the
established inheritance pattern, so this acts as a global toggle.

Currently surfaces one field: `hide_pihole_self_in_top_clients`. New
display preferences should be added here rather than spawning new
single-field endpoints.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.stats import HIDE_SELF_DEFAULT, HIDE_SELF_IN_TOP_CLIENTS_KEY
from app.auth import get_current_user, require_mutation
from app.database import get_db
from app.models.user import User
from app.services.site_settings import (
    get_json_setting,
    get_main_site_id,
    set_json_setting,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/display-settings", tags=["display"])


class DisplaySettingsResponse(BaseModel):
    hide_pihole_self_in_top_clients: bool = HIDE_SELF_DEFAULT


class DisplaySettingsRequest(BaseModel):
    hide_pihole_self_in_top_clients: bool = HIDE_SELF_DEFAULT


@router.get("", response_model=DisplaySettingsResponse)
async def get_display_settings(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DisplaySettingsResponse:
    main_id = await get_main_site_id(db)
    if main_id is None:
        return DisplaySettingsResponse()
    value = await get_json_setting(
        db, main_id, HIDE_SELF_IN_TOP_CLIENTS_KEY, default=HIDE_SELF_DEFAULT,
    )
    return DisplaySettingsResponse(hide_pihole_self_in_top_clients=bool(value))


@router.put("", response_model=DisplaySettingsResponse)
async def save_display_settings(
    req: DisplaySettingsRequest,
    user: User = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> DisplaySettingsResponse:
    main_id = await get_main_site_id(db)
    if main_id is None:
        raise HTTPException(
            status_code=500,
            detail="No Main site found. Run config sync first.",
        )
    try:
        await set_json_setting(
            db, main_id, HIDE_SELF_IN_TOP_CLIENTS_KEY,
            req.hide_pihole_self_in_top_clients,
        )
    except Exception as exc:
        logger.exception("Failed to persist display settings: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save display settings.")
    logger.info(
        "user=%s updated display settings: hide_pihole_self_in_top_clients=%s",
        user.username, req.hide_pihole_self_in_top_clients,
    )
    return DisplaySettingsResponse(
        hide_pihole_self_in_top_clients=req.hide_pihole_self_in_top_clients,
    )
