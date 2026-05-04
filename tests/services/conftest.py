"""Shared setup for service-layer tests.

Service tests in this directory are pure respx — no DB, no FastAPI app.
The session-scoped event loop set in pytest.ini still applies, so async
fixtures don't see cross-loop issues.
"""
from __future__ import annotations
