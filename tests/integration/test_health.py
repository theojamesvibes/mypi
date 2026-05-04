"""Tests for the unauthenticated /api/health endpoint."""
from __future__ import annotations


async def test_health_returns_200_unauthenticated(client):
    """The iOS app calls /api/health before its API key is configured —
    the endpoint must work without any auth header."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200


async def test_health_returns_version_and_intervals(client):
    """Response shape is part of the iOS app's contract — it reads
    `stats_poll_interval` to compute staleness thresholds."""
    resp = await client.get("/api/health")
    body = resp.json()
    assert "version" in body
    assert "stats_poll_interval" in body
    assert "queries_poll_interval" in body
    assert isinstance(body["stats_poll_interval"], int)
    assert isinstance(body["queries_poll_interval"], int)
