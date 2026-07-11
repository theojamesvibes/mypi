# ruff: noqa: F401  — this module exists to re-export the package's surface
"""Background APScheduler jobs that poll Pi-hole instances.

Scheduler structure (Phase 3 onward):
  For each active site, a pair of APScheduler jobs — `poll_stats_site_<id>`
  and `poll_queries_site_<id>` — polls only that site's instances. Adding
  or removing a site updates just its pair via `schedule_site` /
  `unschedule_site`; a poll-interval change reschedules every site's
  queries job via `reschedule_all_queries_jobs`. Sites are independent:
  a hang on one site's poll doesn't delay another's next tick.

  Per-instance module state (watermarks, offline counters, circuit
  breaker) is still keyed by instance id — site scoping doesn't change
  what we track, only which instances we touch per tick. A dedicated
  `prune_inactive_state` job clears dict entries for instances that
  config_loader has deactivated.

  The master-blocklist-delta auto-sync trigger only fires for the Main
  site's master in Phase 3. `sync_service` still has global
  single-master state; Phase 4 rewrites it to be per-site and this
  guard goes away.

Package layout (split from the original 1050-line collector.py, #83):
  state.py       shared per-instance/per-site dicts, `_spawn`, circuit breaker
  instances.py   DB lookup helpers (active instances / sites)
  stats.py       stats poll, offline alerts, per-instance stall detection
  vip.py         VIP-cluster transfer / group-stall state machine
  queries.py     query-log poll, backfill, `_store_queries`
  versions.py    Pi-hole version fetch
  maintenance.py retention cleanup, state pruning, shutdown
  scheduling.py  per-site APScheduler job registration

Everything below is re-exported so existing importers (`app.main`, tests)
keep working unchanged. The state dicts are re-exported *by reference* —
mutating `collector._last_seen_ts` mutates the real dict — but rebinding
an attribute on this package (`monkeypatch.setattr(collector, ...)`) does
NOT affect the implementation modules; patch the owning submodule instead
(e.g. `collector.queries._QUERY_POLL_PAGE_SIZE`, `collector.state._spawn`).
"""
from app.services.collector import (
    instances,
    maintenance,
    queries,
    scheduling,
    state,
    stats,
    versions,
    vip,
)
from app.services.collector.instances import (
    _get_active_instances,
    _get_instances_for_site,
    _get_site_name,
    get_active_site_ids,
)
from app.services.collector.maintenance import (
    cleanup_old_data,
    prune_inactive_state,
    shutdown,
)
from app.services.collector.queries import (
    _QUERY_POLL_MAX_PAGES,
    _QUERY_POLL_PAGE_SIZE,
    _poll_queries_for,
    _store_queries,
    backfill_all_instances,
    backfill_queries_for,
    poll_queries_for_site,
)
from app.services.collector.scheduling import (
    _site_job_id,
    reschedule_all_queries_jobs,
    schedule_site,
    unschedule_site,
)
from app.services.collector.state import (
    _CIRCUIT_COOLDOWN,
    _CIRCUIT_DEDUP_WINDOW,
    _CIRCUIT_FAIL_THRESHOLD,
    _STALL_THRESHOLD_POLLS,
    _VIP_TRANSFER_CONFIRM_POLLS,
    _background_tasks,
    _breaker_allows,
    _breaker_failure,
    _breaker_success,
    _CircuitOpen,
    _consec_failures,
    _cooldown_until,
    _last_failure_at,
    _last_seen_ts,
    _offline_alert_count,
    _offline_retry_count,
    _prev_dns_queries_today,
    _prev_status,
    _prev_watermark_for_stall,
    _site_poll_seq,
    _spawn,
    _stall_alerted,
    _stall_count,
    _vip_active_node,
    _vip_advance_streak,
    _vip_group_stall_alerted,
    _vip_last_advance_seq,
)
from app.services.collector.stats import (
    _check_stalled,
    _instance_advanced,
    _poll_stats_for,
    poll_stats_for_site,
)
from app.services.collector.versions import (
    _fetch_version_for,
    fetch_all_instance_versions,
)
from app.services.collector.vip import _check_vip_state
