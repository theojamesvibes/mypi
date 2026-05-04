"""Unit tests for app/services/query_stream.py — pub/sub fan-out, queue
overflow drops, and the wave-3 subscriber cap."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import query_stream


@pytest.fixture(autouse=True)
def _clear_subscribers():
    """Each test starts with an empty subscriber set; some tests register
    fixtures and rely on isolation."""
    query_stream._subscribers.clear()
    yield
    query_stream._subscribers.clear()


# ── publish basics ───────────────────────────────────────────────────────────


def test_publish_with_no_subscribers_is_noop():
    """Background path must never raise when nobody's listening."""
    query_stream.publish(uuid.uuid4(), uuid.uuid4(), count=5)
    assert query_stream.subscriber_count() == 0


def test_publish_drops_zero_or_negative_counts():
    """Tick events are only meaningful when rows actually committed."""
    async def go():
        async with query_stream.subscribe() as q:
            query_stream.publish(uuid.uuid4(), uuid.uuid4(), count=0)
            query_stream.publish(uuid.uuid4(), uuid.uuid4(), count=-1)
            assert q.empty()
    asyncio.run(go())


# ── subscribe + receive ──────────────────────────────────────────────────────


def test_subscribe_receives_publish_for_matching_site():
    site_a = uuid.uuid4()
    instance = uuid.uuid4()

    async def go():
        async with query_stream.subscribe(site_id=site_a) as q:
            query_stream.publish(instance, site_a, count=1)
            tick = await asyncio.wait_for(q.get(), timeout=1.0)
            assert tick is None  # tick payload is intentionally empty
    asyncio.run(go())


def test_subscribe_with_none_site_receives_every_publish():
    site_a = uuid.uuid4()
    site_b = uuid.uuid4()

    async def go():
        async with query_stream.subscribe(site_id=None) as q:
            query_stream.publish(uuid.uuid4(), site_a, count=1)
            query_stream.publish(uuid.uuid4(), site_b, count=1)
            await asyncio.wait_for(q.get(), timeout=1.0)
            await asyncio.wait_for(q.get(), timeout=1.0)
    asyncio.run(go())


def test_subscribe_with_site_filters_other_sites():
    """A site-scoped subscriber sees only its own publishes."""
    site_a = uuid.uuid4()
    site_b = uuid.uuid4()

    async def go():
        async with query_stream.subscribe(site_id=site_a) as q:
            query_stream.publish(uuid.uuid4(), site_b, count=1)
            # No tick should arrive — assert via a short timeout.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)
    asyncio.run(go())


# ── queue overflow drops cleanly ─────────────────────────────────────────────


def test_publish_drops_when_queue_full_without_raising():
    """Slow consumer's queue fills up; the publish path must never block
    the collector or raise — overflow is silently dropped."""
    site = uuid.uuid4()

    async def go():
        async with query_stream.subscribe(site_id=site) as q:
            # _QUEUE_MAXSIZE is 4 — push more than that to force overflow.
            for _ in range(20):
                query_stream.publish(uuid.uuid4(), site, count=1)
            assert q.qsize() == query_stream._QUEUE_MAXSIZE
    asyncio.run(go())


# ── subscriber cap (wave-3) ──────────────────────────────────────────────────


def test_subscribe_raises_when_cap_reached():
    """The 51st concurrent subscriber is refused so a misbehaving client
    can't chew file descriptors."""
    cap = query_stream._MAX_SUBSCRIBERS

    async def go():
        # Open `cap` simultaneous subscribers and verify the next one fails.
        contexts = [query_stream.subscribe() for _ in range(cap)]
        queues = []
        for cm in contexts:
            queues.append(await cm.__aenter__())

        try:
            assert query_stream.subscriber_count() == cap
            with pytest.raises(query_stream.SubscriberLimitReached):
                async with query_stream.subscribe():
                    pass  # pragma: no cover — should raise on enter
        finally:
            for cm in contexts:
                await cm.__aexit__(None, None, None)

        assert query_stream.subscriber_count() == 0

    asyncio.run(go())


def test_subscriber_count_decrements_after_exit():
    async def go():
        assert query_stream.subscriber_count() == 0
        async with query_stream.subscribe():
            assert query_stream.subscriber_count() == 1
        assert query_stream.subscriber_count() == 0
    asyncio.run(go())
