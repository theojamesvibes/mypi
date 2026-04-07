"""Pi-hole sync service — pushes config from the master instance to all replicas."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models.pihole import PiholeInstance
from app.models.settings import AppSetting
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

# Schedule config (in-memory; resets on restart)
_schedule_minutes: int = 0        # 0 = disabled
_auto_gravity: bool = False       # auto-sync when master blocklist count changes
_schedule_task: asyncio.Task | None = None
_last_blocklist_count: int | None = None

# Saved sync options reused for scheduled/auto runs
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


async def load_schedule() -> None:
    """Load persisted schedule from the database (called at startup)."""
    global _schedule_minutes, _auto_gravity, _sync_opts
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(AppSetting, "sync_schedule")
        if row and row.value:
            data = json.loads(row.value)
            _schedule_minutes = data.get("interval_minutes", 0)
            _auto_gravity = data.get("auto_gravity", False)
            _sync_opts = {
                "import_config": data.get("import_config", True),
                "import_gravity": data.get("import_gravity", True),
                "import_dhcp_leases": data.get("import_dhcp_leases", False),
                "run_gravity": data.get("run_gravity", True),
            }
            if _schedule_minutes > 0:
                import asyncio as _a
                loop = _a.get_event_loop()
                if loop.is_running():
                    loop.create_task(_start_schedule_task(_schedule_minutes))
            logger.info(
                "Loaded sync schedule from DB: interval=%d min, auto_gravity=%s",
                _schedule_minutes, _auto_gravity,
            )
    except Exception as exc:
        logger.warning("Could not load sync schedule from DB: %s", exc)


async def _persist_schedule() -> None:
    """Save current schedule to the database."""
    data = json.dumps({
        "interval_minutes": _schedule_minutes,
        "auto_gravity": _auto_gravity,
        **_sync_opts,
    })
    try:
        async with AsyncSessionLocal() as db:
            stmt = pg_insert(AppSetting).values(key="sync_schedule", value=data)
            stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": data})
            await db.execute(stmt)
            await db.commit()
    except Exception as exc:
        logger.warning("Could not persist sync schedule: %s", exc)


async def _start_schedule_task(minutes: int) -> None:
    global _schedule_task
    if _schedule_task and not _schedule_task.done():
        _schedule_task.cancel()
    _schedule_task = asyncio.get_event_loop().create_task(_scheduled_loop(minutes))


async def _scheduled_loop(minutes: int) -> None:
    """Background task that runs sync every `minutes` minutes."""
    import asyncio as _asyncio
    while True:
        await _asyncio.sleep(minutes * 60)
        if _lock.locked():
            continue  # skip if a manual sync is already running
        logger.info("Scheduled sync triggered (every %d min)", minutes)
        await run_sync(**_sync_opts)


def set_schedule(
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
        _schedule_task = asyncio.get_event_loop().create_task(
            _scheduled_loop(interval_minutes)
        )
        logger.info("Sync scheduled every %d minutes.", interval_minutes)
    else:
        logger.info("Sync schedule disabled.")

    asyncio.get_event_loop().create_task(_persist_schedule())


async def notify_blocklist_count(count: int) -> None:
    """Called by the stats collector after each master snapshot.
    Triggers an auto-sync if the blocklist count changed and auto_gravity is on."""
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
            asyncio.get_event_loop().create_task(run_sync(**_sync_opts))
    else:
        _last_blocklist_count = count


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

            # Teleporter import can be slow on low-powered Pi hardware — use a
            # generous timeout so we don't abort mid-transfer.
            SYNC_TIMEOUT = 300.0  # 5 minutes

            # Export from master using a fresh (non-persistent) client
            async with PiholeClient(master.url, master.api_password, timeout=SYNC_TIMEOUT) as client:
                zip_data = await client.get_teleporter()
            logger.info("Exported teleporter from master %s (%d bytes)", master.name, len(zip_data))

            # Push to each replica concurrently
            async def _sync_replica(replica: PiholeInstance) -> InstanceSyncResult:
                try:
                    async with PiholeClient(replica.url, replica.api_password, timeout=SYNC_TIMEOUT) as client:
                        await client.post_teleporter(
                            zip_data,
                            import_config=import_config,
                            import_gravity=import_gravity,
                            import_dhcp_leases=import_dhcp_leases,
                        )
                        if run_gravity:
                            await client.run_gravity()
                    logger.info("Sync to %s succeeded", replica.name)
                    return InstanceSyncResult(name=replica.name, status="success")
                except Exception as exc:
                    logger.warning("Sync to %s failed: %s", replica.name, exc)
                    return InstanceSyncResult(name=replica.name, status="error", error=str(exc))

            results = list(await asyncio.gather(*[_sync_replica(r) for r in replicas]))

            # Optionally run gravity on master too
            if run_gravity:
                try:
                    async with PiholeClient(master.url, master.api_password, timeout=SYNC_TIMEOUT) as client:
                        await client.run_gravity()
                    logger.info("Gravity update triggered on master %s", master.name)
                except Exception as exc:
                    logger.warning("Gravity on master failed (non-fatal): %s", exc)

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

        return _state
