from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyCreate(BaseModel):
    name: str = Field(..., max_length=128)
    is_read_only: bool = False


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_read_only: bool = False
    created_at: datetime
    last_used_at: datetime | None = None

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyResponse):
    """Returned once on creation — includes the raw key."""
    raw_key: str


class UserResponse(BaseModel):
    id: uuid.UUID
    username: str
    email: str | None = None
    is_active: bool
    created_at: datetime
    # True iff the current request was authenticated with a read-only
    # API key. Allows iOS / automation clients to render mutation
    # actions (Block domain, Sync now, etc.) appropriately without
    # having to discover their scope by trial-and-error 403.
    # Always False for password / cookie / JWT auth.
    is_read_only: bool = False

    model_config = {"from_attributes": True}
