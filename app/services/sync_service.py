"""Pi-hole sync service — pushes config from the master instance to replicas.

Phase 4b: sync is per-site. Every public entry point takes an optional
`site_id` keyword (defaulting to the active Main site) so the existing API
surface keeps working verbatim. Internally, schedule config, in-flight
locks, last-result state, and blocklist-delta watermarks are all dict
keyed by `str(site_id)`. A site's sync picks its master and replicas
only from that site's active instances — two sites' syncs can run
concurrently without contention.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import ssl
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.models.site import Site
from app.services import pushover as pushover_service
from app.services.client_manager import close_client, get_client, save_sid
from app.services.site_settings import get_main_site_id, get_setting, set_setting

# Transient socket failures that the collector already self-heals by evicting
# the persistent client.  Sync's _sync_replica uses the same class list to
# retry post_teleporter once on idle-keepalive half-opens.
_TRANSIENT_SYNC_ERRORS = (ssl.SSLError, httpx.ConnectError, httpx.RemoteProtocolError)

logger = logging.getLogger(__name__)


def _ftl_series(version: str | None) -> str | None:
    """Reduce an FTL version string to its `major.minor` series for comparison.

    Pi-hole's teleporter archive is versioned with FTL: importing a master
    export into a replica on a different minor series can be rejected or
    silently mis-mapped. We compare on `major.minor` (not patch) so a replica
    one patch behind during a rolling upgrade doesn't trip a warning, while a
    genuine 6.0 → 6.1 schema gap does. Returns None when the version is unknown
    (never polled) so the caller can skip the check rather than warn on noise.
    """
    if not version:
        return None
    parts = version.lstrip("v").split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return f"{parts[0]}.{parts[1]}"


@dataclass
class InstanceSyncResult:
    name: str
    status: Literal["success", "error"]
    error: str | None = None
    # None / "master" / "replica" — surfaced so the sync-result UI can pill
    # the row alongside the per-replica status icon.
    vip_role: str | None = None


@dataclass
class SyncState:
    status: Literal["idle", "running", "success", "error"] = "idle"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    master: str | None = None
    # None / "master" / "replica" — for the master row in the sync-result UI.
    # Independent of `is_master`: a sync master may or may not also be the
    # currently-active VIP node.
    master_vip_role: str | None = None
    results: list[InstanceSyncResult] = field(default_factory=list)
    error: str | None = None


# Per-site state — keyed by str(site_id). Accessor helpers create dict entries
# on demand so each site starts fresh from hard-coded defaults the first time
# it's touched, then overlays anything restored from site_settings.
_state_by_site: dict[str, SyncState] = {}
_lock_by_site: dict[str, asyncio.Lock] = {}
_schedule_task_by_site: dict[str, asyncio.Task] = {}
_last_blocklist_by_site: dict[str, int] = {}

# Per-site schedule config: the JSON payload stored under
# site_settings.sync_schedule, unpacked into a plain dict. Fields:
#   interval_minutes: int   (0 = disabled)
#   auto_gravity:     bool
#   import_config:    bool
#   import_gravity:   bool
#   import_dhcp_leases: bool
#   run_gravity:      bool
_schedule_by_site: dict[str, dict] = {}

# Fire-and-forget background tasks. asyncio keeps only weak refs to bare
# create_task(...) — stash each task here and log any exception so a
# failed notify/persist doesn't silently vanish.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.exception("sync_service background task failed", exc_info=exc)

    task.add_done_callback(_done)


# ── Site resolution + state accessors ─────────────────────────────────────────

async def _resolve_site_id(site_id: uuid.UUID | None) -> uuid.UUID:
    """None → active Main site's id. Raises if no Main exists."""
    if site_id is not None:
        return site_id
    async with AsyncSessionLocal() as db:
        main_id = await get_main_site_id(db)
    if main_id is None:
        raise RuntimeError("No Main site configured — sync_service cannot resolve target.")
    return main_id


def _get_lock(site_id: uuid.UUID) -> asyncio.Lock:
    key = str(site_id)
    if key not in _lock_by_site:
        _lock_by_site[key] = asyncio.Lock()
    return _lock_by_site[key]


def _get_state_dict(site_id: uuid.UUID) -> SyncState:
    return _state_by_site.setdefault(str(site_id), SyncState())


def _get_schedule_config(site_id: uuid.UUID) -> dict:
    return _schedule_by_site.setdefault(str(site_id), {
        "interval_minutes": 0,
        "auto_gravity": False,
        "import_config": True,
        "import_gravity": True,
        "import_dhcp_leases": False,
        "run_gravity": True,
    })


async def get_state(site_id: uuid.UUID | None = None) -> SyncState:
    """Return the most-recent sync state for a site (Main if site_id omitted)."""
    sid = await _resolve_site_id(site_id)
    return _get_state_dict(sid)


async def get_schedule(site_id: uuid.UUID | None = None) -> dict:
    """Return the current schedule config for a site (Main if site_id omitted)."""
    sid = await _resolve_site_id(site_id)
    return dict(_get_schedule_config(sid))


# ── DB persistence ────────────────────────────────────────────────────────────

async def _db_upsert_site(site_id: uuid.UUID, key: str, value: str) -> None:
    """Write `key`/`value` into site_settings for `site_id` and verify it landed."""
    async with AsyncSessionLocal() as db:
        # set_setting does upsert + fresh-session read-back verification.
        await set_setting(db, site_id, key, value)
    logger.info("DB upsert verified: site=%s key='%s'", site_id, key)


async def _persist_schedule(site_id: uuid.UUID) -> None:
    cfg = _get_schedule_config(site_id)
    await _db_upsert_site(site_id, "sync_schedule", json.dumps({
        "interval_minutes": cfg["interval_minutes"],
        "auto_gravity": cfg["auto_gravity"],
        "import_config": cfg["import_config"],
        "import_gravity": cfg["import_gravity"],
        "import_dhcp_leases": cfg["import_dhcp_leases"],
        "run_gravity": cfg["run_gravity"],
    }))
    logger.info("Sync schedule persisted for site %s.", site_id)


async def _persist_sync_state(site_id: uuid.UUID, state: SyncState) -> None:
    """Store the site's last completed sync result so it survives restarts."""
    try:
        payload = {
            "status": state.status,
            "completed_at": state.completed_at.isoformat() if state.completed_at else None,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "master": state.master,
            "master_vip_role": state.master_vip_role,
            "error": state.error,
            "results": [
                {"name": r.name, "status": r.status, "error": r.error, "vip_role": r.vip_role}
                for r in state.results
            ],
        }
        await _db_upsert_site(site_id, "sync_last_result", json.dumps(payload))
    except Exception as exc:
        logger.warning("Could not persist sync state for site %s: %s", site_id, exc)


async def load_schedule() -> None:
    """Iterate every active site and restore its schedule + last result.

    Called once at startup. Re-arms each site's interval loop if that site
    had a non-zero `interval_minutes` persisted.
    """
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Site)
                .where(Site.is_active.is_(True))
                .order_by(Site.sort_order, Site.name)
            )
            sites = list(result.scalars().all())
    except Exception as exc:
        logger.error("Failed to enumerate sites for sync schedule load: %s", exc)
        return

    for site in sites:
        await _load_site_schedule(site.id, site.name)


async def _load_site_schedule(site_id: uuid.UUID, site_name: str) -> None:
    sid_key = str(site_id)
    schedule_raw: str | None = None
    result_raw: str | None = None
    try:
        async with AsyncSessionLocal() as db:
            schedule_raw = await get_setting(db, site_id, "sync_schedule")
            result_raw = await get_setting(db, site_id, "sync_last_result")
    except Exception as exc:
        logger.error("Failed to load sync settings for site %s: %s", site_name, exc)
        return

    if schedule_raw:
        try:
            data = json.loads(schedule_raw)
            cfg = _get_schedule_config(site_id)
            cfg["interval_minutes"] = data.get("interval_minutes", 0)
            cfg["auto_gravity"] = data.get("auto_gravity", False)
            cfg["import_config"] = data.get("import_config", True)
            cfg["import_gravity"] = data.get("import_gravity", True)
            cfg["import_dhcp_leases"] = data.get("import_dhcp_leases", False)
            cfg["run_gravity"] = data.get("run_gravity", True)
            logger.warning(
                "STARTUP: loaded sync schedule for site '%s' — interval=%d min, auto_gravity=%s",
                site_name, cfg["interval_minutes"], cfg["auto_gravity"],
            )
        except Exception as exc:
            logger.error("STARTUP: could not parse sync_schedule for site %s: %s", site_name, exc)
    else:
        logger.info("STARTUP: no sync_schedule row for site '%s' — using defaults.", site_name)

    if result_raw:
        try:
            data = json.loads(result_raw)
            _state_by_site[sid_key] = SyncState(
                status=data.get("status", "idle"),
                started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
                completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
                master=data.get("master"),
                master_vip_role=data.get("master_vip_role"),
                error=data.get("error"),
                results=[
                    InstanceSyncResult(
                        name=r["name"], status=r["status"], error=r.get("error"),
                        vip_role=r.get("vip_role"),
                    )
                    for r in data.get("results", [])
                ],
            )
            logger.info(
                "Restored last sync state for site '%s': %s at %s",
                site_name, _state_by_site[sid_key].status, _state_by_site[sid_key].completed_at,
            )
        except Exception as exc:
            logger.warning("Could not parse last sync result for site %s: %s", site_name, exc)

    cfg = _get_schedule_config(site_id)
    if cfg["interval_minutes"] > 0:
        # Re-arm the site's interval loop. Stash the task in
        # _schedule_task_by_site so set_schedule can cancel it on
        # reconfiguration (otherwise a startup-armed loop and a
        # user-armed loop would run concurrently after the first PUT).
        task = asyncio.create_task(_scheduled_loop(site_id, site_name, cfg["interval_minutes"]))
        _schedule_task_by_site[sid_key] = task
        logger.info("Re-armed sync schedule for site '%s': every %d min.", site_name, cfg["interval_minutes"])


# ── Schedule management ───────────────────────────────────────────────────────

async def _scheduled_loop(site_id: uuid.UUID, site_name: str, minutes: int) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        # The loop must survive any single-iteration failure: it is spawned
        # with a bare create_task (no done-callback logging), so an escaped
        # exception kills scheduled syncs for this site silently until
        # restart. Known trigger: the lock.locked() pre-check passes, a
        # user-triggered sync grabs the lock during our DB awaits, and
        # run_sync raises "sync already in progress". CancelledError is
        # deliberately not caught — set_schedule cancels us on reconfigure.
        try:
            lock = _get_lock(site_id)
            if lock.locked():
                continue
            logger.info("Scheduled sync triggered for site '%s' (every %d min)", site_name, minutes)
            cfg = _get_schedule_config(site_id)
            opts = {k: cfg[k] for k in ("import_config", "import_gravity", "import_dhcp_leases", "run_gravity")}
            await run_sync(site_id=site_id, **opts)
        except Exception:
            logger.exception(
                "Scheduled sync iteration failed for site '%s'; will retry in %d min.",
                site_name, minutes,
            )


async def set_schedule(
    interval_minutes: int,
    auto_gravity: bool,
    import_config: bool,
    import_gravity: bool,
    import_dhcp_leases: bool,
    run_gravity: bool,
    site_id: uuid.UUID | None = None,
) -> None:
    sid = await _resolve_site_id(site_id)
    sid_key = str(sid)
    cfg = _get_schedule_config(sid)
    cfg["interval_minutes"] = interval_minutes
    cfg["auto_gravity"] = auto_gravity
    cfg["import_config"] = import_config
    cfg["import_gravity"] = import_gravity
    cfg["import_dhcp_leases"] = import_dhcp_leases
    cfg["run_gravity"] = run_gravity

    existing = _schedule_task_by_site.get(sid_key)
    if existing and not existing.done():
        existing.cancel()
        _schedule_task_by_site.pop(sid_key, None)

    if interval_minutes > 0:
        site_name = await _lookup_site_name(sid)
        task = asyncio.create_task(_scheduled_loop(sid, site_name, interval_minutes))
        _schedule_task_by_site[sid_key] = task
        logger.info("Sync scheduled for site %s every %d min.", site_name, interval_minutes)
    else:
        logger.info("Sync schedule disabled for site %s.", sid)

    await _persist_schedule(sid)


async def notify_blocklist_count(site_id: uuid.UUID, count: int) -> None:
    sid_key = str(site_id)
    cfg = _get_schedule_config(site_id)
    if not cfg["auto_gravity"]:
        _last_blocklist_by_site[sid_key] = count
        return
    last = _last_blocklist_by_site.get(sid_key)
    if last is not None and count != last:
        logger.info(
            "Master blocklist count for site %s changed %d → %d; triggering auto-sync.",
            site_id, last, count,
        )
        _last_blocklist_by_site[sid_key] = count
        lock = _get_lock(site_id)
        if not lock.locked():
            opts = {k: cfg[k] for k in ("import_config", "import_gravity", "import_dhcp_leases", "run_gravity")}
            _spawn(run_sync(site_id=site_id, **opts))
    else:
        _last_blocklist_by_site[sid_key] = count


async def _lookup_site_name(site_id: uuid.UUID) -> str:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Site.name).where(Site.id == site_id))
        name = result.scalar_one_or_none()
    return name or str(site_id)


# ── Sync execution ────────────────────────────────────────────────────────────

async def run_sync(
    import_config: bool = True,
    import_gravity: bool = True,
    import_dhcp_leases: bool = False,
    run_gravity: bool = True,
    site_id: uuid.UUID | None = None,
) -> SyncState:
    sid = await _resolve_site_id(site_id)
    sid_key = str(sid)
    site_name = await _lookup_site_name(sid)
    lock = _get_lock(sid)

    if lock.locked():
        raise RuntimeError(f"A sync is already in progress for site '{site_name}'.")

    async with lock:
        _state_by_site[sid_key] = SyncState(status="running", started_at=datetime.now(UTC))
        results: list[InstanceSyncResult] = []

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(PiholeInstance).where(
                        PiholeInstance.site_id == sid,
                        PiholeInstance.is_active.is_(True),
                    )
                )
                instances = list(result.scalars().all())

            master = next((i for i in instances if i.is_master), None)
            replicas = [i for i in instances if not i.is_master]

            if not master:
                raise ValueError(
                    f"No master instance configured for site '{site_name}'. "
                    "Add 'master: true' to one entry in the site's instance list and restart."
                )
            if not replicas:
                raise ValueError(f"No replica instances to sync to in site '{site_name}'.")

            logger.info(
                "Sync started: site=%s master=%s, replicas=%s, config=%s, gravity=%s, dhcp=%s, run_gravity=%s",
                site_name, master.name, [r.name for r in replicas],
                import_config, import_gravity, import_dhcp_leases, run_gravity,
            )

            # Pre-flight: warn on FTL minor-series drift between master and
            # replicas. The teleporter archive is versioned with FTL, so a
            # cross-series import can be rejected or mis-mapped. We warn rather
            # than block: a one-patch lag during a rolling upgrade is harmless,
            # and refusing to sync could leave replicas staler than a
            # best-effort import would. Uses the version_ftl already persisted by
            # the collector (refreshed at startup and after every sync), so this
            # costs no extra Pi-hole round-trips. (Upstream nebula-sync #223
            # blocks outright; we surface the risk and proceed.)
            master_series = _ftl_series(master.version_ftl)
            for r in replicas:
                replica_series = _ftl_series(r.version_ftl)
                if master_series and replica_series and master_series != replica_series:
                    logger.warning(
                        "FTL version drift: master %s is on FTL %s but replica %s "
                        "is on FTL %s. Teleporter import may be rejected or partial "
                        "across minor versions — align Pi-hole versions if the sync fails.",
                        master.name, master.version_ftl, r.name, r.version_ftl,
                    )

            # Step 1: Run gravity on master to get fresh blocklists before export.
            try:
                master_client = await get_client(master)
                await master_client.run_gravity()
                await save_sid(master.id, master_client.sid)
                logger.info("Gravity update completed on master %s before export", master.name)
            except Exception as exc:
                logger.warning("Gravity on master failed (non-fatal, continuing with export): %s", exc)

            # Step 2: Export teleporter zip from master.
            zip_data: bytes | None = None
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    master_client = await get_client(master)
                    zip_data = await master_client.get_teleporter()
                    await save_sid(master.id, master_client.sid)
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == 0:
                        logger.warning(
                            "Teleporter export from %s failed (attempt 1), retrying in 2s: %s",
                            master.name, exc,
                        )
                        await asyncio.sleep(2)
            if zip_data is None:
                raise RuntimeError(
                    f"Failed to export teleporter from master {master.name}: {last_exc}"
                ) from last_exc
            logger.info("Exported teleporter from master %s (%d bytes)", master.name, len(zip_data))

            _validate_teleporter_zip(zip_data)
            logger.info("Teleporter ZIP from master %s passed validation", master.name)

            # Step 3: Push to each replica concurrently, then run gravity on each.
            async def _sync_replica(replica: PiholeInstance) -> InstanceSyncResult:
                key = str(replica.id)
                try:
                    try:
                        replica_client = await get_client(replica)
                        await replica_client.post_teleporter(
                            zip_data,
                            import_config=import_config,
                            import_gravity=import_gravity,
                            import_dhcp_leases=import_dhcp_leases,
                        )
                        await save_sid(replica.id, replica_client.sid)
                    except _TRANSIENT_SYNC_ERRORS as exc:
                        logger.warning(
                            "Transient connection error syncing to %s (%s: %s) — "
                            "evicting client and retrying once",
                            replica.name, type(exc).__name__, exc,
                        )
                        await close_client(key)
                        replica_client = await get_client(replica)
                        await replica_client.post_teleporter(
                            zip_data,
                            import_config=import_config,
                            import_gravity=import_gravity,
                            import_dhcp_leases=import_dhcp_leases,
                        )
                        await save_sid(replica.id, replica_client.sid)
                    logger.info("Teleporter import to %s succeeded", replica.name)

                    if import_gravity:
                        await asyncio.sleep(5)
                        try:
                            replica_client = await get_client(replica)
                            await replica_client.run_gravity()
                            await save_sid(replica.id, replica_client.sid)
                            logger.info("Gravity update completed on replica %s", replica.name)
                        except Exception as g_exc:
                            logger.warning("Gravity on replica %s failed (non-fatal): %s", replica.name, g_exc)

                    logger.info("Sync to %s succeeded", replica.name)
                    return InstanceSyncResult(
                        name=replica.name, status="success", vip_role=replica.vip_role,
                    )
                except Exception as exc:
                    logger.warning("Sync to %s failed: %s", replica.name, exc)
                    return InstanceSyncResult(
                        name=replica.name,
                        status="error",
                        error=str(exc),
                        vip_role=replica.vip_role,
                    )

            results = list(await asyncio.gather(*[_sync_replica(r) for r in replicas]))

            overall: Literal["success", "error"] = (
                "error" if any(r.status == "error" for r in results) else "success"
            )
            _state_by_site[sid_key] = SyncState(
                status=overall,
                started_at=_state_by_site[sid_key].started_at,
                completed_at=datetime.now(UTC),
                master=master.name,
                master_vip_role=master.vip_role,
                results=results,
            )

        except Exception as exc:
            logger.error("Sync failed for site '%s': %s", site_name, exc)
            _state_by_site[sid_key] = SyncState(
                status="error",
                started_at=_state_by_site[sid_key].started_at,
                completed_at=datetime.now(UTC),
                error=str(exc),
                results=results,
            )

        current_state = _state_by_site[sid_key]
        _spawn(_persist_sync_state(sid, current_state))
        if current_state.status == "error":
            if current_state.error:
                _spawn(pushover_service.notify_sync_failure(
                    current_state.error, site_name=site_name, site_id=sid,
                ))
            else:
                failed = [r for r in current_state.results if r.status == "error"]
                if failed:
                    body = "; ".join(
                        f"{r.name}: {r.error or 'unknown error'}" for r in failed
                    )
                    _spawn(pushover_service.notify_sync_failure(
                        body, site_name=site_name, site_id=sid,
                    ))
        # Refresh Pi-hole version info after sync — FTL may have restarted on
        # replicas after the teleporter import.
        _spawn(_refresh_versions_post_sync())
        return current_state


def _validate_teleporter_zip(data: bytes) -> None:
    """Sanity-check the master's teleporter export before broadcasting it.

    Pi-hole v6's teleporter endpoint is the single commit point on each
    replica — there is no server-side staging or rollback. If the master
    hands back a corrupt or empty archive, we would overwrite every
    replica's working config with garbage. This check is the guard that
    keeps a bad export from fanning out.

    Minimal bytes: Pi-hole v6 teleporters contain pihole.toml plus the
    gravity schema and are never below ~1 KB in practice; anything
    smaller is a truncated or empty response.
    """
    min_bytes = 1024
    if len(data) < min_bytes:
        raise RuntimeError(
            f"Teleporter export from master is only {len(data)} bytes "
            f"(expected ≥{min_bytes}) — refusing to broadcast a likely-truncated archive"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise RuntimeError(
                    f"Teleporter ZIP member '{bad_member}' failed CRC — archive is corrupt"
                )
            infos = zf.infolist()
            if not infos:
                raise RuntimeError("Teleporter ZIP contains no members")
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed == 0:
                raise RuntimeError("Teleporter ZIP members are all zero-length")
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Teleporter export is not a valid ZIP archive: {exc}") from exc


async def _refresh_versions_post_sync() -> None:
    await asyncio.sleep(15)
    from app.services.collector import fetch_all_instance_versions
    await fetch_all_instance_versions()
