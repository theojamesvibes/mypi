"""Shared setup for service-layer tests.

Service tests in this directory are pure respx — no DB, no FastAPI app.
The session-scoped event loop set in pytest.ini still applies, so async
fixtures don't see cross-loop issues.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_collector_state():
    """The collector module keeps per-instance + per-site state in
    bare module-level dicts. Clear them between tests so a circuit-
    breaker trip in one case can't poison a fresh-state assertion in
    the next."""
    from app.services import collector

    state_dicts = (
        collector._consec_failures,
        collector._cooldown_until,
        collector._last_failure_at,
        collector._last_seen_ts,
        collector._prev_status,
        collector._offline_retry_count,
        collector._offline_alert_count,
        collector._stall_count,
        collector._stall_alerted,
        collector._prev_dns_queries_today,
        collector._prev_watermark_for_stall,
        collector._vip_last_advance_seq,
        collector._vip_prev_count,
        collector._vip_lead_streak,
        collector._vip_active_node,
        collector._vip_group_stall_alerted,
        collector._site_poll_seq,
    )
    for d in state_dicts:
        d.clear()
    yield
    for d in state_dicts:
        d.clear()
