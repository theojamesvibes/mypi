"""Pi-hole sync service — pushes config from the master instance to all replicas."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.models.settings import AppSetting
from app.services import pushover as pushover_service
from app.services.pihole_client import PiholeClient

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

    # ── Startup diagnostic: dump entire app_settings table via raw SQL ─────────
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text("SELECT key, value FROM app_settings"))).all()
        if rows:
            logger.warning("STARTUP: app_settings table contains %d row(s): %s",
                           len(rows), [r[0] for r in rows])
        else:
            logger.warning("STARTUP: app_settings table is EMPTY — no persisted settings found")
    except Exception as exc:
        logger.error("STARTUP: could not read app_settings table: %s — "
                     "check that migration 0004 ran and DB is accessible", exc)
        return

    # ── Verify DB is writable by round-tripping a test key ────────────────────
    try:
        test_val = "startup_write_test"
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("INSERT INTO app_settings (key, value) VALUES (:k, :v) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                {"k": "_startup_test", "v": test_val},
            )
            await db.commit()
        async with AsyncSessionLocal() as db:
            result = (await db.execute(
                text("SELECT value FROM app_settings WHERE key = '_startup_test'")
            )).scalar_one_or_none()
        if result != test_val:
            logger.error("STARTUP: DB write/read-back test FAILED — wrote %r, read back %r. "
                         "Settings will NOT persist.", test_val, result)
            return
        logger.warning("STARTUP: DB write/read-back test passed — database is writable")
    except Exception as exc:
        logger.error("STARTUP: DB write test raised exception: %s", exc)
        return

    # ── Load actual settings ───────────────────────────────────────────────────
    try:
        async with AsyncSessionLocal() as db:
            schedule_row = await db.get(AppSetting, "sync_schedule")
            result_row = await db.get(AppSetting, "sync_last_result")
    except Exception as exc:
        logger.error("STARTUP: failed to load settings from DB: %s", exc)
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

    # Re-arm interval task if schedule was active
    if _schedule_minutes > 0:
        asyncio.create_task(_scheduled_loop(_schedule_minutes))
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
            asyncio.create_task(run_sync(**_sync_opts))
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

            # Teleporter import can be slow on low-powered Pi hardware
            SYNC_TIMEOUT = 300.0  # 5 minutes

            # Step 1: Run gravity on master to get fresh blocklists before export
            try:
                async with PiholeClient(master.url, master.api_password, timeout=SYNC_TIMEOUT) as client:
                    await client.run_gravity()
                logger.info("Gravity update completed on master %s before export", master.name)
            except Exception as exc:
                logger.warning("Gravity on master failed (non-fatal, continuing with export): %s", exc)

            # Step 2: Export teleporter zip from master (contains fresh gravity DB)
            async with PiholeClient(master.url, master.api_password, timeout=SYNC_TIMEOUT) as client:
                zip_data = await client.get_teleporter()
            logger.info("Exported teleporter from master %s (%d bytes)", master.name, len(zip_data))

            # Step 3: Push to each replica concurrently, then run gravity on each.
            # The teleporter ZIP carries the master's adlists and domain lists, but the
            # compiled gravity table is rebuilt by Pi-hole FTL during a gravity run.
            # Without running gravity on replicas after import, their domain counts stay
            # stale and diverge from the master.
            async def _sync_replica(replica: PiholeInstance) -> InstanceSyncResult:
                try:
                    async with PiholeClient(replica.url, replica.api_password, timeout=SYNC_TIMEOUT) as client:
                        await client.post_teleporter(
                            zip_data,
                            import_config=import_config,
                            import_gravity=import_gravity,
                            import_dhcp_leases=import_dhcp_leases,
                        )
                    logger.info("Teleporter import to %s succeeded", replica.name)

                    if import_gravity:
                        # FTL restarts after the teleporter import; give it a moment
                        # before connecting again to run gravity.
                        await asyncio.sleep(5)
                        try:
                            async with PiholeClient(replica.url, replica.api_password, timeout=SYNC_TIMEOUT) as client:
                                await client.run_gravity()
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

        asyncio.create_task(_persist_sync_state(_state))
        if _state.status == "error":
            asyncio.create_task(
                pushover_service.notify_sync_failure(_state.error or "One or more replicas failed to sync")
            )
        return _state
