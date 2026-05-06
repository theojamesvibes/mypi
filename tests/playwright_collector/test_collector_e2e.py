"""End-to-end tests for the collector → DB → dashboard pipeline.

These tests rely on the Pi-hole emulator (tests/emulators/pihole/app.py)
running on 127.0.0.1:9876 and the MyPi app subprocess polling it. The
emulator returns deterministic stats (5000 / 1250 / 175000) and a
rotating set of synthetic queries; tests assert that data flows through
the dashboard correctly.

Why poll-and-wait instead of fixed sleeps: the first poll lands ~5 s
after app start (STATS_POLL_INTERVAL=5), but rendering is bound by the
slower of the next poll cycle and the dashboard JS's own fetch — so we
poll the UI for a non-empty value with a generous timeout rather than
sleeping a hardcoded amount.
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_dashboard_renders_emulator_stats(
    authed_page: Page, base_url: str, wait_for_first_poll: bool,
):
    """Stats cards bind to numbers polled from the emulator.

    The emulator returns:
      total = 5000, blocked = 1250, percent_blocked = 25.0,
      domains_being_blocked = 175000

    However MyPi's /api/stats/summary computes total/blocked from
    QueryLog aggregates over the time window (24h by default), not
    from the StatsSnapshot. So we assert on `domains_on_blocklist`
    (taken straight from the latest snapshot) and on the queries
    table contents which come from the emulator's /api/queries.
    """
    page = authed_page
    page.goto(f"{base_url}/")

    # Wait for the dashboard JS to populate any stat card. "—" is the
    # initial template state; once the API responds, the value updates.
    expect(page.locator("#total-queries")).not_to_have_text(
        "—", timeout=15_000,
    )
    # Blocklist size comes from the latest StatsSnapshot row written
    # by the collector — the emulator hardcodes 175,000.
    expect(page.locator("#blocklist-size")).to_have_text(
        "175,000", timeout=15_000,
    )


def test_instances_table_lists_emulator(
    authed_page: Page, base_url: str, wait_for_first_poll: bool,
):
    """The instances table shows the configured Pi-hole + its status."""
    page = authed_page
    page.goto(f"{base_url}/")
    # Instance name comes from pihole_instances.yml in this suite.
    expect(page.locator("#instances-tbody")).to_contain_text(
        "Emu-Pi-1", timeout=15_000,
    )


def test_queries_page_shows_emulator_queries(
    authed_page: Page, base_url: str, wait_for_first_poll: bool,
):
    """The /queries page lists rows pulled from /api/queries.

    The emulator returns domains under .emu.test — at least one such
    domain should appear in the rendered table. Allow a longer window
    here because queries poll on a separate (faster) cadence and the
    Playwright JS-driven fetch may need a tick to refresh.
    """
    page = authed_page
    page.goto(f"{base_url}/queries")
    expect(page.locator("body")).to_contain_text(
        ".emu.test", timeout=20_000,
    )


def test_collector_writes_stats_snapshots(
    live_app, base_url: str, db_engine, wait_for_first_poll: bool,
):
    """Direct DB assertion: at least one StatsSnapshot row exists with
    the emulator's domains_on_blocklist value.

    Uses the live engine rather than a Playwright page so a polling-
    pipeline regression that affects the DB but not the UI rendering
    is still caught.
    """
    from sqlalchemy import text

    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT domains_on_blocklist, dns_queries_today "
                "FROM stats_snapshots ORDER BY collected_at DESC LIMIT 1"
            )
        ).first()

    assert row is not None, "Collector did not write a StatsSnapshot"
    assert row.domains_on_blocklist == 175000, (
        f"Expected blocklist size 175,000 from the emulator; "
        f"got {row.domains_on_blocklist}"
    )
    # Total queries comes from the emulator's stats summary too.
    assert row.dns_queries_today == 5000


def test_collector_writes_query_logs(
    live_app, base_url: str, db_engine, wait_for_first_poll: bool,
):
    """The query backfill should land emulator queries in query_logs."""
    from sqlalchemy import text

    # The query poll fires every 2 s; give it a few cycles to land
    # rows after the first stats poll completes.
    import time as _time
    deadline = _time.monotonic() + 20.0
    count = 0
    while _time.monotonic() < deadline:
        with db_engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM query_logs "
                    "WHERE domain LIKE '%.emu.test'"
                )
            ).scalar() or 0
        if count > 0:
            break
        _time.sleep(0.5)

    assert count > 0, "No emulator query rows found in query_logs"
