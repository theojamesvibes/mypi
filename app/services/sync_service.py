"""Pi-hole sync service — pushes config from the master instance to all replicas."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.models.settings import AppSetting
from app.services import pushover as pushover_service
from app.services.client_manager import get_client, save_sid

logger = logging.getLogger(__name__)


@dataclass
class InstanceSyncResult:
    name: str
    status: Literal["success", "error"]
    error: str | None = None


@dataclass
class SyncState:
    status: Literal["idle", "running", "success", "error"] = "idle"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    master: str | None = None
    results: list[InstanceSyncResult] = field(default_factory=list)
    error: str | None = None


_state = SyncState()
_lock = asyncio.Lock()

# Schedule config — persisted to DB, restored on startup
_schedule_minutes: int = 0
_auto_gravity: bool = False
_schedule_task: asyncio.Task | None = None
_last_blocklist_count: int | None = None

# Fire-and-forget background tasks spawned from this module. asyncio only
# keeps weak references to tasks, so without stashing them here they can be
# GC'd mid-run. `_spawn` adds a task to this set and removes it on completion,
# logging any uncaught exception.
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

_sync_opts: dict = {
    "import_config": True,
    "import_gravity": True,
    "import_dhcp_leases": False,
    "run_gravity": True,
}


def get_state() -> SyncState:
    return _state


def get_schedule() -> dict:
    return {
        "interval_minutes": _schedule_minutes,
        "auto_gravity": _auto_gravity,
        **_sync_opts,
    }


# ── DB persistence ────────────────────────────────────────────────────────────

async def _db_upsert(key: str, value: str) -> None:
    """Write key/value to app_settings and verify it was committed."""
    async with AsyncSessionLocal() as db:
        stmt = (
            pg_insert(AppSetting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await db.execute(stmt)
        await db.commit()

    # Verify in a fresh session (no identity-map cache) that the row landed
    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, key)
        if row is None or row.value != value:
            raise RuntimeError(
                f"DB write verification failed for key '{key}': "
                f"committed but read-back returned {'nothing' if row is None else repr(row.value)}"
            )
    logger.info("DB upsert verified: key='%s'", key)


async def _persist_schedule() -> None:
    await _db_upsert("sync_schedule", json.dumps({
        "interval_minutes": _schedule_minutes,
        "auto_gravity": _auto_gravity,
        **_sync_opts,
    }))
    logger.info("Sync schedule persisted and verified in DB.")


async def _persist_sync_state(state: SyncState) -> None:
    """Store the last completed sync result so it survives restarts."""
    try:
        payload = {
            "status": state.status,
            "completed_at": state.completed_at.isoformat() if state.completed_at else None,
            "started_at": state.started_at.isoformat() if state.started_at else None,
            "master": state.master,
            "error": state.error,
            "results": [{"name": r.name, "status": r.status, "error": r.error} for r in state.results],
        }
        await _db_upsert("sync_last_result", json.dumps(payload))
    except Exception as exc:
        logger.warning("Could not persist sync state: %s", exc)


async def load_schedule() -> None:
    """Load persisted schedule and last sync result from DB (called at startup)."""
    global _schedule_minutes, _auto_gravity, _sync_opts, _state

    try:
        async with AsyncSessionLocal() as db:
            schedule_row = await db.get(AppSetting, "sync_schedule")
            result_row = await db.get(AppSetting, "sync_last_result")
    except Exception as exc:
        logger.error("Failed to load sync settings from DB: %s", exc)
        return

    # Restore schedule
    if schedule_row and schedule_row.value:
        try:
            data = json.loads(schedule_row.value)
            _schedule_minutes = data.get("interval_minutes", 0)
            _auto_gravity = data.get("auto_gravity", False)
            _sync_opts = {
                "import_config": data.get("import_config", True),
                "import_gravity": data.get("import_gravity", True),
                "import_dhcp_leases": data.get("import_dhcp_leases", False),
                "run_gravity": data.get("run_gravity", True),
            }
            logger.warning(
                "STARTUP: loaded sync schedule from DB — interval=%d min, auto_gravity=%s",
                _schedule_minutes, _auto_gravity,
            )
        except Exception as exc:
            logger.error("STARTUP: could not parse sync schedule JSON: %s", exc)
    else:
        logger.warning("STARTUP: no sync_schedule row in DB — using defaults (interval=0, disabled)")

    # Restore last sync result
    if result_row and result_row.value:
        try:
            data = json.loads(result_row.value)
            _state = SyncState(
                status=data.get("status", "idle"),
                started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
                completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
                master=data.get("master"),
                error=data.get("error"),
                results=[
                    InstanceSyncResult(name=r["name"], status=r["status"], error=r.get("error"))
                    for r in data.get("results", [])
                ],
            )
            logger.info("Restored last sync state from DB: %s at %s", _state.status, _state.completed_at)
        except Exception as exc:
            logger.warning("Could not parse last sync result: %s", exc)
    else:
        logger.info("No persisted sync result found in DB.")

    # Re-arm interval task if schedule was active. Stash the task in
    # _schedule_task so set_schedule(...) can cancel it on reconfiguration
    # (without this, a startup-armed loop and a user-armed loop would run
    # concurrently after the first PUT /api/sync/schedule).
    if _schedule_minutes > 0:
        global _schedule_task
        _schedule_task = asyncio.create_task(_scheduled_loop(_schedule_minutes))
        logger.info("Re-armed sync schedule: every %d minutes.", _schedule_minutes)


# ── Schedule management ───────────────────────────────────────────────────────

async def _scheduled_loop(minutes: int) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        if _lock.locked():
            continue
        logger.info("Scheduled sync triggered (every %d min)", minutes)
        await run_sync(**_sync_opts)


async def set_schedule(
    interval_minutes: int,
    auto_gravity: bool,
    import_config: bool,
    import_gravity: bool,
    import_dhcp_leases: bool,
    run_gravity: bool,
) -> None:
    global _schedule_minutes, _auto_gravity, _schedule_task, _sync_opts
    _schedule_minutes = interval_minutes
    _auto_gravity = auto_gravity
    _sync_opts = {
        "import_config": import_config,
        "import_gravity": import_gravity,
        "import_dhcp_leases": import_dhcp_leases,
        "run_gravity": run_gravity,
    }

    if _schedule_task and not _schedule_task.done():
        _schedule_task.cancel()
        _schedule_task = None

    if interval_minutes > 0:
        _schedule_task = asyncio.create_task(_scheduled_loop(interval_minutes))
        logger.info("Sync scheduled every %d minutes.", interval_minutes)
    else:
        logger.info("Sync schedule disabled.")

    await _persist_schedule()


async def notify_blocklist_count(count: int) -> None:
    global _last_blocklist_count
    if not _auto_gravity:
        _last_blocklist_count = count
        return
    if _last_blocklist_count is not None and count != _last_blocklist_count:
        logger.info(
            "Master blocklist count changed %d → %d; triggering auto-sync.",
            _last_blocklist_count, count,
        )
        _last_blocklist_count = count
        if not _lock.locked():
            # _spawn keeps a strong ref + logs uncaught exceptions; bare
            # asyncio.create_task drops the result and risks GC mid-run.
            _spawn(run_sync(**_sync_opts))
    else:
        _last_blocklist_count = count


# ── Sync execution ────────────────────────────────────────────────────────────

async def run_sync(
    import_config: bool = True,
    import_gravity: bool = True,
    import_dhcp_leases: bool = False,
    run_gravity: bool = True,
) -> SyncState:
    global _state

    if _lock.locked():
        raise RuntimeError("A sync is already in progress.")

    async with _lock:
        _state = SyncState(status="running", started_at=datetime.now(timezone.utc))
        results: list[InstanceSyncResult] = []

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(PiholeInstance).where(PiholeInstance.is_active.is_(True))
                )
                instances = list(result.scalars().all())

            master = next((i for i in instances if i.is_master), None)
            replicas = [i for i in instances if not i.is_master]

            if not master:
                raise ValueError(
                    "No master instance configured. "
                    "Add 'master: true' to one entry in pihole_instances.yml and restart."
                )
            if not replicas:
                raise ValueError("No replica instances to sync to.")

            logger.info(
                "Sync started: master=%s, replicas=%s, config=%s, gravity=%s, dhcp=%s, run_gravity=%s",
                master.name, [r.name for r in replicas],
                import_config, import_gravity, import_dhcp_leases, run_gravity,
            )

            # Step 1: Run gravity on master to get fresh blocklists before export.
            # Uses the shared persistent client — no new session created.
            try:
                master_client = await get_client(master)
                await master_client.run_gravity()
                await save_sid(master.id, master_client.sid)
                logger.info("Gravity update completed on master %s before export", master.name)
            except Exception as exc:
                logger.warning("Gravity on master failed (non-fatal, continuing with export): %s", exc)

            # Step 2: Export teleporter zip from master (contains fresh gravity DB).
            # Retry once with a short delay: gravity's socket framing quirks can
            # occasionally leave stale bytes that corrupt the first teleporter read.
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

            # Validate the master's export before broadcasting it. If the
            # archive is truncated, zero-length, or corrupt, we fail the whole
            # sync here so no replica overwrites its working config with
            # garbage.
            _validate_teleporter_zip(zip_data)
            logger.info("Teleporter ZIP from master %s passed validation", master.name)

            # Step 3: Push to each replica concurrently, then run gravity on each.
            # The teleporter ZIP carries the master's adlists and domain lists, but the
            # compiled gravity table is rebuilt by Pi-hole FTL during a gravity run.
            # Without running gravity on replicas after import, their domain counts stay
            # stale and diverge from the master.
            async def _sync_replica(replica: PiholeInstance) -> InstanceSyncResult:
                try:
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
                        # FTL restarts after the teleporter import; give it a moment
                        # before reconnecting to run gravity.  The SID will have been
                        # invalidated by the restart so the client will re-authenticate
                        # automatically on the next request.
                        await asyncio.sleep(5)
                        try:
                            replica_client = await get_client(replica)
                            await replica_client.run_gravity()
                            await save_sid(replica.id, replica_client.sid)
                            logger.info("Gravity update completed on replica %s", replica.name)
                        except Exception as g_exc:
                            # Gravity failure is non-fatal: adlists are synced; the
                            # domain count will catch up when Pi-hole runs gravity on
                            # its own schedule.
                            logger.warning("Gravity on replica %s failed (non-fatal): %s", replica.name, g_exc)

                    logger.info("Sync to %s succeeded", replica.name)
                    return InstanceSyncResult(name=replica.name, status="success")
                except Exception as exc:
                    logger.warning("Sync to %s failed: %s", replica.name, exc)
                    return InstanceSyncResult(name=replica.name, status="error", error=str(exc))

            results = list(await asyncio.gather(*[_sync_replica(r) for r in replicas]))

            overall: Literal["success", "error"] = (
                "error" if any(r.status == "error" for r in results) else "success"
            )
            _state = SyncState(
                status=overall,
                started_at=_state.started_at,
                completed_at=datetime.now(timezone.utc),
                master=master.name,
                results=results,
            )

        except Exception as exc:
            logger.error("Sync failed: %s", exc)
            _state = SyncState(
                status="error",
                started_at=_state.started_at,
                completed_at=datetime.now(timezone.utc),
                error=str(exc),
                results=results,
            )

        _spawn(_persist_sync_state(_state))
        if _state.status == "error":
            _spawn(
                pushover_service.notify_sync_failure(_state.error or "One or more replicas failed to sync")
            )
        # Refresh Pi-hole version info after sync — FTL may have restarted on
        # replicas after the teleporter import, so give it a moment to come back.
        _spawn(_refresh_versions_post_sync())
        return _state


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
