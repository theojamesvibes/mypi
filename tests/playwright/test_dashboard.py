"""Dashboard renders real-looking content from the seeded database.

The `seed_data` fixture writes a site, an instance, 25 hourly stats
snapshots, and 200 query logs into the test Postgres before the test
body runs. The dashboard JS then fetches /api/stats/summary, /history,
and /top, populates the stat cards and tables, and we assert on what's
visible.

Numbers in the assertions match what the API actually computes — totals
come from QueryLog aggregates over the time window (200 rows, every 4th
blocked = 50), not from the StatsSnapshot rows. The blocklist size on
the fourth card *does* come from the latest snapshot (125,000 in seed).
"""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_dashboard_shows_seeded_totals(
    logged_in_page: Page, base_url: str, seed_data,
):
    page = logged_in_page
    page.goto(f"{base_url}/")

    # The dashboard JS waits on three API calls in parallel; until they
    # resolve every stat card shows "—". Polling for the "—" → number
    # transition is flake-resistant because it tolerates whatever order
    # the cards happen to fill in.
    total = page.locator("#total-queries")
    expect(total).not_to_have_text("—", timeout=10_000)

    # 200 seeded query logs in the last hour, every 4th flagged BLOCKED
    # (50 blocked / 200 total = 25%). Numbers come back through
    # toLocaleString() so 1,000+ values get a comma — under 1,000 like
    # 200 don't.
    expect(total).to_have_text("200", timeout=10_000)
    expect(page.locator("#queries-blocked")).to_have_text("50", timeout=10_000)
    expect(page.locator("#percent-blocked")).to_have_text("25.0%", timeout=10_000)
    # Blocklist size lives on the latest StatsSnapshot row.
    expect(page.locator("#blocklist-size")).to_have_text("125,000", timeout=10_000)


def test_dashboard_lists_seeded_instance(
    logged_in_page: Page, base_url: str, seed_data,
):
    page = logged_in_page
    page.goto(f"{base_url}/")
    # The instances table is populated from the same /stats/summary call.
    expect(page.locator("#instances-tbody")).to_contain_text(
        seed_data["instance_name"], timeout=10_000,
    )


def test_dashboard_top_blocked_populated(
    logged_in_page: Page, base_url: str, seed_data,
):
    page = logged_in_page
    page.goto(f"{base_url}/")
    # The blocked-domain table fills from /stats/top. Seed cycles 6
    # domains; every 4th query is blocked, so several distinct domains
    # show up in the blocked-list rollup.
    blocked_table = page.locator("#top-blocked")
    expect(blocked_table).to_contain_text("example", timeout=10_000)


def test_queries_page_lists_seeded_rows(
    logged_in_page: Page, base_url: str, seed_data,
):
    page = logged_in_page
    page.goto(f"{base_url}/queries")
    # The query log table populates from /api/queries. We don't assert
    # on the exact table id (it can vary by template), just that one of
    # the seeded domains lands somewhere on the page.
    expect(page.locator("body")).to_contain_text(
        "example.com", timeout=10_000,
    )
