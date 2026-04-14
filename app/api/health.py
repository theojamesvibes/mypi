from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])

_version_file = Path(__file__).parent.parent.parent / "VERSION"


@router.get("/api/health", include_in_schema=True)
async def health():
    """Unauthenticated health/discovery endpoint.

    Used by the iOS app to verify connectivity and retrieve poll intervals
    before an API key is configured, and to compute staleness thresholds.
    """
    version = _version_file.read_text().strip() if _version_file.exists() else "dev"
    return {
        "version": version,
        "stats_poll_interval": settings.stats_poll_interval,
        "queries_poll_interval": settings.queries_poll_interval,
    }
