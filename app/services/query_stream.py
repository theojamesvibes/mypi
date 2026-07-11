"""In-process pub/sub for live query-row events.

Replaces the dashboard's 2-second `setInterval` poll for the /queries
"Live" toggle: the collector publishes a tick whenever it commits new
rows, an SSE endpoint streams those ticks to subscribed clients, and
each tick triggers a single client-side refetch via the existing
`/api/queries` endpoint. Net effect — the frontend only refetches when
data has actually changed instead of every 2s regardless.

We deliberately stream "tick" events (no payload) rather than streaming
serialized rows. Reasons:
  - The /queries view has rich filtering / sorting / pagination wired
    through the existing endpoint; replicating all of it in a streaming
    context is a lot of code for marginal benefit.
  - Tick events are tiny, so a noisy Pi-hole doesn't blow up the SSE
    bandwidth budget.
  - The client-side debouncing is trivial — drop ticks while a refetch
    is already in-flight.

Subscriber queues are bounded (`_QUEUE_MAXSIZE`) so a slow consumer can
never backpressure the collector. When the queue is full, publish drops
silently — the next tick that fits still triggers a refetch, so no real
data is lost (the row is in Postgres either way).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Per-subscriber queue depth. 4 ticks of headroom is plenty; the consumer
# only ever cares about "did anything happen since my last refetch" so
# more depth would just delay the publish-drop point without changing
# behaviour.
_QUEUE_MAXSIZE = 4

# Hard cap on simultaneous subscribers across the whole process. A single
# browser session opens 1–3 SSE connections (dashboard + queries +
# combined views in tabs); 50 is several normal users with several tabs
# each. Past that, an authenticated client opening connections in a loop
# would chew file descriptors and asyncio task slots — refuse new
# connections cleanly so the live view falls back to polling.
_MAX_SUBSCRIBERS = 50


class SubscriberLimitReached(RuntimeError):
    """Raised by `subscribe()` when `_MAX_SUBSCRIBERS` is already in use."""


class _Subscriber:
    """One open SSE connection. `site_id=None` means cross-site (the
    Combined view or the unfiltered global /api/queries/stream); a UUID
    scopes the subscription to ticks from instances under that site."""

    __slots__ = ("queue", "site_id")

    def __init__(self, site_id: uuid.UUID | None) -> None:
        self.queue: asyncio.Queue[None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self.site_id = site_id


_subscribers: set[_Subscriber] = set()


def publish(instance_id: uuid.UUID, site_id: uuid.UUID, count: int) -> None:
    """Notify subscribers that `count` new rows just committed for
    `instance_id` in `site_id`. Cheap fan-out; bounded queues drop on
    overflow. Sync — safe to call from the collector's async context
    without `await`."""
    if count <= 0:
        return
    for sub in list(_subscribers):
        if sub.site_id is not None and sub.site_id != site_id:
            continue
        try:
            sub.queue.put_nowait(None)
        except asyncio.QueueFull:
            # Slow consumer — drop. The next tick that fits triggers a
            # refetch, so this isn't data loss; it just means a moment of
            # slightly-staler UI.
            pass
        except Exception as exc:
            # Defensive: a publish failure must NEVER bubble back into
            # _store_queries and break the collector's commit path.
            logger.warning("query_stream publish failed: %s", exc)


@asynccontextmanager
async def subscribe(
    site_id: uuid.UUID | None = None,
) -> AsyncIterator[asyncio.Queue[None]]:
    """Register an SSE consumer's queue. Caller owns the queue for the
    duration of the `async with`; cleanup runs when the context exits
    (including on cancellation when the client disconnects).

    Raises `SubscriberLimitReached` when the global cap is already in
    use — the SSE route translates this into an `error` event so the
    client falls back to polling without the connection looking dead.
    """
    if len(_subscribers) >= _MAX_SUBSCRIBERS:
        raise SubscriberLimitReached(
            f"SSE subscriber cap of {_MAX_SUBSCRIBERS} reached"
        )
    sub = _Subscriber(site_id)
    _subscribers.add(sub)
    try:
        yield sub.queue
    finally:
        _subscribers.discard(sub)


def subscriber_count() -> int:
    """Useful for diagnostics / future status endpoints."""
    return len(_subscribers)
