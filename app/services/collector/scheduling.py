"""APScheduler job registration: one stats+queries poll pair per site."""
from __future__ import annotations

import logging
import uuid

from app.services.collector.queries import poll_queries_for_site
from app.services.collector.stats import poll_stats_for_site

logger = logging.getLogger(__name__)


def _site_job_id(site_id: uuid.UUID, kind: str) -> str:
    """Job id for a site's poll pair. `kind` is 'stats' or 'queries'."""
    return f"poll_{kind}_site_{site_id}"


def schedule_site(
    scheduler,
    site_id: uuid.UUID,
    stats_interval_seconds: int,
    queries_interval_seconds: int,
) -> None:
    """Register (or replace) the stats+queries poll pair for a site."""
    scheduler.add_job(
        poll_stats_for_site,
        "interval",
        seconds=stats_interval_seconds,
        args=[site_id],
        id=_site_job_id(site_id, "stats"),
        replace_existing=True,
    )
    scheduler.add_job(
        poll_queries_for_site,
        "interval",
        seconds=queries_interval_seconds,
        args=[site_id],
        id=_site_job_id(site_id, "queries"),
        replace_existing=True,
    )
    logger.info(
        "Scheduled site %s (stats=%ds, queries=%ds)",
        site_id, stats_interval_seconds, queries_interval_seconds,
    )


def unschedule_site(scheduler, site_id: uuid.UUID) -> None:
    """Remove the stats+queries poll pair for a site. No-op if not registered."""
    removed = 0
    for kind in ("stats", "queries"):
        job_id = _site_job_id(site_id, kind)
        try:
            scheduler.remove_job(job_id)
            removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Unscheduled site %s (removed %d job(s))", site_id, removed)


def reschedule_all_queries_jobs(scheduler, new_interval_seconds: int) -> None:
    """Apply a new queries-poll interval to every registered site's queries job.

    Called from `poll_settings.set_reschedule_callback` when an operator
    changes the global query-poll interval. Phase 4 will add per-site
    interval overrides via site_settings; until then, the interval is
    applied uniformly.
    """
    rescheduled = 0
    for job in scheduler.get_jobs():
        if job.id.startswith("poll_queries_site_"):
            scheduler.reschedule_job(job.id, trigger="interval", seconds=new_interval_seconds)
            rescheduled += 1
    logger.info(
        "Rescheduled %d site queries-poll job(s) to %ds interval",
        rescheduled, new_interval_seconds,
    )
