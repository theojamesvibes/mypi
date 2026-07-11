"""Tests for the small persistence-helper services that the
notifications/sync/poll/session settings pages all rely on:
session_settings, poll_settings, site_settings."""
from __future__ import annotations

# ── session_settings (timeout) ───────────────────────────────────────────────


async def test_session_timeout_round_trip(db_session):
    from app.services import session_settings

    # Default is no-timeout (0). Save a non-zero value.
    await session_settings.save_settings(60)
    assert session_settings.get_timeout_minutes() == 60

    # Reload from DB simulates a fresh container.
    await session_settings.load_settings()
    assert session_settings.get_timeout_minutes() == 60


async def test_session_timeout_effective_minutes_clamps_zero():
    """0 means 'no timeout' in the API but the JWT still needs an
    expiry — `effective_minutes` clamps 0 to a long horizon."""
    from app.services import session_settings

    # 0 → some sensible long value (not literal 0).
    eff = session_settings.effective_minutes(0)
    assert eff > 0


# ── poll_settings ────────────────────────────────────────────────────────────


async def test_poll_settings_get_default_interval():
    from app.services import poll_settings as ps_service
    interval = ps_service.get_interval_seconds()
    assert isinstance(interval, int)
    assert interval > 0


async def test_poll_settings_save_persists_and_reloads(db_session, monkeypatch):
    from app.models.site import Site
    from app.services import poll_settings as ps_service

    # save_settings requires a Main site (intervals are stored under it).
    db_session.add(Site(
        name="Main", slug="main", is_main=True, is_active=True, sort_order=0,
    ))
    await db_session.commit()

    captured = []
    ps_service.set_reschedule_callback(lambda new_seconds: captured.append(new_seconds))

    await ps_service.save_settings(45)
    assert ps_service.get_interval_seconds() == 45
    assert captured == [45]

    # Reload simulates a fresh container.
    await ps_service.load_settings()
    assert ps_service.get_interval_seconds() == 45


# ── site_settings (per-site key/value with Main inheritance) ─────────────────


async def test_site_settings_set_and_get_round_trip(db_session):
    from app.models.site import Site
    from app.services import site_settings

    site = Site(name="A", slug="a", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    await site_settings.set_setting(db_session, site.id, "test_key", "test_value")

    val = await site_settings.get_setting(db_session, site.id, "test_key")
    assert val == "test_value"


async def test_site_settings_get_returns_none_for_missing_key(db_session):
    from app.models.site import Site
    from app.services import site_settings

    site = Site(name="A", slug="a", is_main=True, is_active=True, sort_order=0)
    db_session.add(site)
    await db_session.commit()
    await db_session.refresh(site)

    val = await site_settings.get_setting(db_session, site.id, "nope")
    assert val is None


async def test_get_main_site_id_returns_active_main(db_session):
    from app.models.site import Site
    from app.services import site_settings

    main = Site(name="M", slug="m", is_main=True, is_active=True, sort_order=0)
    other = Site(name="O", slug="o", is_main=False, is_active=True, sort_order=1)
    db_session.add_all([main, other])
    await db_session.commit()
    await db_session.refresh(main)

    main_id = await site_settings.get_main_site_id(db_session)
    assert main_id == main.id


async def test_get_main_site_id_returns_none_when_no_main(db_session):
    from app.services import site_settings
    main_id = await site_settings.get_main_site_id(db_session)
    assert main_id is None
