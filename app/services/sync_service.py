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
import copy
import io
import json
import logging
import re
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
from app.services.site_settings import (
    clear_setting,
    get_main_site_id,
    get_setting,
    set_setting,
)

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


# ── Per-key config exclusions ────────────────────────────────────────────────
#
# A teleporter import with config=True replaces a replica's entire
# pihole.toml, so anything host-specific on that replica — its local DNS
# records, its upstreams, its DHCP range — is silently replaced by the
# master's copy. Pi-hole's teleporter API has no notion of a partial config
# import, so we bracket the import instead: snapshot the excluded keys off
# the replica first, then PATCH them back through /api/config, which does
# have proper partial-update semantics.
#
# Keys are dotted paths into the config tree, e.g. "dns.hosts" (local DNS
# records), "dns.cnameRecords", "dns.upstreams", or a whole section like
# "dhcp".

# Upper bound on how many keys one site may pin. Well past any real use;
# it exists so a malformed client payload can't make every sync walk a
# thousand-entry list against every replica.
MAX_CONFIG_EXCLUSIONS = 50

# Dotted path of TOML bare keys — what Pi-hole's config tree actually uses.
_EXCLUSION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")

# Seconds to let FTL settle after a teleporter import before PATCHing the
# preserved keys back. FTL restarts itself on config import; the same 5 s
# the gravity step already waits is enough for it to be serving again, and
# the client re-authenticates by itself if the SID did not survive.
_CONFIG_RESTORE_SETTLE_SECONDS = 5

# Waits between further restore attempts when the replica is unreachable.
# The settle above is a guess at how long FTL's restart takes; on a busy Pi
# (or one hit by two imports back to back) it is still down at 5 s, and a
# single refused connection used to cost the replica its pinned keys. About
# a minute in total — far longer than any FTL restart seen in practice.
_CONFIG_RESTORE_RETRY_DELAYS: tuple[float, ...] = (5, 10, 15, 30)

# site_settings key holding one replica's pre-import snapshot of its pinned
# keys. Written before the import and cleared only once the restore has been
# verified, so a restore that fails outright is finished by the next sync
# instead of that sync snapshotting the master's values as the replica's own.
_PENDING_RESTORE_KEY = "sync_pending_restore:{}"


def normalise_exclusions(raw: object) -> list[str]:
    """Clean a caller-supplied exclusion list into canonical dotted key paths.

    Order-preserving and de-duplicated. Malformed entries are dropped with a
    warning rather than rejected outright: a typo in one key should not cost
    the user the other keys they asked to keep.
    """
    if not isinstance(raw, list | tuple):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        key = item.strip().strip(".")
        if not key or not _EXCLUSION_KEY_RE.match(key):
            logger.warning("Ignoring malformed config exclusion key %r", item)
            continue
        if key not in out:
            out.append(key)
    if len(out) > MAX_CONFIG_EXCLUSIONS:
        logger.warning(
            "Config exclusion list truncated from %d to %d keys.",
            len(out), MAX_CONFIG_EXCLUSIONS,
        )
        out = out[:MAX_CONFIG_EXCLUSIONS]
    return out


def _pluck(tree: dict, path: str) -> tuple[bool, object]:
    """Walk a dotted path into a config tree. Returns (found, value)."""
    node: object = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _nest(path: str, value: object) -> dict:
    """Inverse of _pluck: "dns.hosts" + v -> {"dns": {"hosts": v}}."""
    parts = path.split(".")
    out: dict = {parts[-1]: value}
    for part in reversed(parts[:-1]):
        out = {part: out}
    return out


def _merge_into(dst: dict, src: dict) -> dict:
    """Deep-merge src into dst so sibling keys under one section coexist."""
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge_into(dst[key], value)
        else:
            dst[key] = value
    return dst


def build_preserve_payload(cfg: dict, paths: list[str]) -> tuple[dict, list[str]]:
    """Pick `paths` out of a replica's config tree into a PATCH-shaped partial.

    Returns the partial plus the paths actually found, so the caller can
    report what it is really keeping. A path missing from the tree (a typo,
    or a key this Pi-hole version doesn't have) is skipped with a warning —
    there is nothing to preserve, and failing the whole sync over it would
    be worse than syncing the rest.
    """
    payload: dict = {}
    found: list[str] = []
    for path in paths:
        ok, value = _pluck(cfg, path)
        if not ok:
            logger.warning(
                "Config exclusion '%s' is not present in this replica's config - skipping.",
                path,
            )
            continue
        # Deep-copy: overlapping paths (say "dhcp" and "dhcp.active") would
        # otherwise have _merge_into write back into the replica's own parsed
        # config tree, and the payload is meant to be an independent snapshot.
        _merge_into(payload, _nest(path, copy.deepcopy(value)))
        found.append(path)
    return payload, found


def recover_pending_restore(
    payload: dict,
    found: list[str],
    pending: dict,
    master_cfg: dict | None,
    exclusions: list[str],
    replica_name: str,
) -> tuple[dict, list[str]]:
    """Fold an unfinished restore from an earlier sync into this one's snapshot.

    `pending` is the replica's own values as snapshotted before an import
    whose restore never verified. Right now the replica may be serving the
    master's copy of those keys, so the live snapshot in `payload` cannot be
    trusted for them. A key is taken from `pending` only when the replica's
    live value equals the master's — the signature of an import that was
    never undone. Anything else means the restore did land, or the user has
    since edited the key by hand, and the live value stands. If the master's
    config could not be read there is no way to tell, and the saved value
    wins: it is known to be the replica's own, the live one is not.
    """
    saved = pending.get("payload")
    paths = pending.get("paths")
    if not isinstance(saved, dict) or not isinstance(paths, list):
        return payload, found
    found = list(found)
    for path in paths:
        if path not in exclusions:
            continue  # the user has since un-pinned this key
        saved_ok, saved_value = _pluck(saved, path)
        if not saved_ok:
            continue
        live_ok, live_value = _pluck(payload, path)
        if live_ok and live_value == saved_value:
            continue
        if master_cfg is not None and live_ok:
            master_ok, master_value = _pluck(master_cfg, path)
            if not master_ok or live_value != master_value:
                continue
        logger.warning(
            "Recovering '%s' on %s from the snapshot saved before an earlier "
            "import whose restore did not complete.",
            path, replica_name,
        )
        _merge_into(payload, _nest(path, copy.deepcopy(saved_value)))
        if path not in found:
            found.append(path)
    return payload, found


@dataclass
class InstanceSyncResult:
    name: str
    status: Literal["success", "error"]
    error: str | None = None
    # None / "master" / "replica" — surfaced so the sync-result UI can pill
    # the row alongside the per-replica status icon.
    vip_role: str | None = None
    # Config keys this replica kept as its own through the import (see
    # `config_exclusions`). Reported so the UI can show that a replica's
    # local DNS really did survive, rather than leaving the user to check
    # each Pi by hand.
    preserved_keys: list[str] = field(default_factory=list)


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
_state_by_site: dict[str, SyncState] = {}          # last sync outcome per site
_lock_by_site: dict[str, asyncio.Lock] = {}        # prevents two syncs of the same site at once
_schedule_task_by_site: dict[str, asyncio.Task] = {}  # the running interval-loop task per site
_last_blocklist_by_site: dict[str, int] = {}       # master's last-seen blocklist size per site

# Per-site schedule config: the JSON payload stored under
# site_settings.sync_schedule, unpacked into a plain dict. Fields:
#   interval_minutes: int   (0 = disabled)
#   auto_gravity:     bool
#   import_config:    bool
#   import_gravity:   bool
#   import_dhcp_leases: bool
#   run_gravity:      bool
#   config_exclusions: list[str]  (dotted pihole.toml keys replicas keep)
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
        "config_exclusions": [],
    })


# The run_sync keyword arguments a schedule config carries, with the same
# defaults _get_schedule_config seeds. Read with .get() on purpose: this
# runs from the collector's auto-sync path and from the interval loop, and
# a schedule dict that predates a new option must not KeyError there and
# take automatic syncing down with it.
_SYNC_OPT_DEFAULTS: dict[str, object] = {
    "import_config": True,
    "import_gravity": True,
    "import_dhcp_leases": False,
    "run_gravity": True,
    "config_exclusions": [],
}


def _sync_opts(cfg: dict) -> dict:
    return {k: cfg.get(k, default) for k, default in _SYNC_OPT_DEFAULTS.items()}


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


async def _load_pending_restore(replica: PiholeInstance) -> dict | None:
    """Return the snapshot left behind by a restore that never verified."""
    async with AsyncSessionLocal() as db:
        raw = await get_setting(db, replica.site_id, _PENDING_RESTORE_KEY.format(replica.id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring unparseable pending config restore for %s.", replica.name)
        return None
    return data if isinstance(data, dict) else None


async def _save_pending_restore(
    replica: PiholeInstance, payload: dict, paths: list[str],
) -> None:
    await _db_upsert_site(
        replica.site_id,
        _PENDING_RESTORE_KEY.format(replica.id),
        json.dumps({
            "payload": payload,
            "paths": paths,
            "saved_at": datetime.now(UTC).isoformat(),
        }),
    )


async def _clear_pending_restore(replica: PiholeInstance) -> None:
    try:
        async with AsyncSessionLocal() as db:
            await clear_setting(db, replica.site_id, _PENDING_RESTORE_KEY.format(replica.id))
    except Exception as exc:
        # Harmless if it lingers: the next sync only acts on it for keys
        # where the replica is still serving the master's value.
        logger.warning("Could not clear pending config restore for %s: %s", replica.name, exc)


async def _persist_schedule(site_id: uuid.UUID) -> None:
    cfg = _get_schedule_config(site_id)
    await _db_upsert_site(site_id, "sync_schedule", json.dumps({
        "interval_minutes": cfg["interval_minutes"],
        "auto_gravity": cfg["auto_gravity"],
        "import_config": cfg["import_config"],
        "import_gravity": cfg["import_gravity"],
        "import_dhcp_leases": cfg["import_dhcp_leases"],
        "run_gravity": cfg["run_gravity"],
        "config_exclusions": cfg["config_exclusions"],
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
                {
                    "name": r.name, "status": r.status, "error": r.error,
                    "vip_role": r.vip_role, "preserved_keys": r.preserved_keys,
                }
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
            cfg["config_exclusions"] = normalise_exclusions(data.get("config_exclusions"))
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
                        preserved_keys=r.get("preserved_keys") or [],
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
            await run_sync(site_id=site_id, **_sync_opts(cfg))
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
    config_exclusions: list[str] | None = None,
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
    cfg["config_exclusions"] = normalise_exclusions(config_exclusions)

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
    """Called after each master poll. If auto-gravity is on and the master's
    blocklist size changed since last time, kick off a sync automatically."""
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
            _spawn(run_sync(site_id=site_id, **_sync_opts(cfg)))
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
    config_exclusions: list[str] | None = None,
) -> SyncState:
    """Push the master Pi-hole's configuration out to all replicas for one site.

    Order: (1) refresh the master's blocklists, (2) export its full config as a
    ZIP, (3) validate the ZIP, (4) import it into every replica at once and
    refresh their blocklists. A per-site lock ensures only one sync per site
    runs at a time. Returns the final SyncState (success/error plus the
    per-replica results).

    `config_exclusions` names dotted `pihole.toml` keys each replica keeps as
    its own — typically its local DNS records. Because the teleporter import
    is all-or-nothing on config, those keys are snapshotted off the replica
    beforehand and PATCHed back afterwards; see the module's "Per-key config
    exclusions" note. Ignored unless `import_config` is set, since nothing
    overwrites them otherwise. Defaults to the site's saved list when None,
    so a caller that doesn't know about exclusions cannot accidentally drop
    a replica's pinned keys.
    """
    sid = await _resolve_site_id(site_id)
    sid_key = str(sid)
    site_name = await _lookup_site_name(sid)
    lock = _get_lock(sid)

    # None means "whatever this site has saved" — the iOS app and any older
    # API client omit the field, and silently syncing with no exclusions
    # would overwrite exactly the keys the user pinned in the UI.
    exclusions = (
        normalise_exclusions(_get_schedule_config(sid).get("config_exclusions"))
        if config_exclusions is None
        else normalise_exclusions(config_exclusions)
    )
    if not import_config:
        exclusions = []

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
                "Sync started: site=%s master=%s, replicas=%s, config=%s, gravity=%s, "
                "dhcp=%s, run_gravity=%s, keep_local=%s",
                site_name, master.name, [r.name for r in replicas],
                import_config, import_gravity, import_dhcp_leases, run_gravity,
                ", ".join(exclusions) or "nothing",
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
            # Handles one replica: tries the import; if it fails with a transient
            # network hiccup (a dropped keepalive socket), it throws away the
            # connection, reconnects, and tries exactly once more before giving up.
            # The master's config tree, read at most once per sync and only
            # when some replica has an unfinished restore to reconcile.
            master_cfg_memo: list[dict | None] = []
            master_cfg_lock = asyncio.Lock()

            async def _master_config() -> dict | None:
                async with master_cfg_lock:
                    if not master_cfg_memo:
                        try:
                            client = await get_client(master)
                            master_cfg_memo.append(await client.get_config())
                            await save_sid(master.id, client.sid)
                        except Exception as exc:
                            logger.warning(
                                "Could not read config from master %s to reconcile a "
                                "pending restore: %s", master.name, exc,
                            )
                            master_cfg_memo.append(None)
                    return master_cfg_memo[0]

            async def _sync_replica(replica: PiholeInstance) -> InstanceSyncResult:
                key = str(replica.id)
                preserve_payload: dict = {}
                preserved: list[str] = []
                try:
                    # Snapshot the keep-local keys *before* the import runs.
                    # The import is the point of no return — a replica whose
                    # local DNS we failed to capture would lose it with no way
                    # back — so a failure here skips that replica's import
                    # entirely rather than pressing on and hoping.
                    if exclusions:
                        try:
                            replica_client = await get_client(replica)
                            cfg_before = await replica_client.get_config()
                            await save_sid(replica.id, replica_client.sid)
                            preserve_payload, preserved = build_preserve_payload(
                                cfg_before, exclusions,
                            )
                        except Exception as exc:
                            raise RuntimeError(
                                f"Could not read config from {replica.name} to preserve "
                                f"{', '.join(exclusions)} — skipping this replica's import "
                                f"rather than overwriting keys we cannot restore: {exc}"
                            ) from exc

                        # An earlier sync may have imported and then failed to
                        # put this replica's keys back, in which case what we
                        # just snapshotted is the master's copy. Persist the
                        # snapshot before importing so the same cannot happen
                        # to this run. Both are a safety net over the DB: if
                        # the DB is unavailable the sync still proceeds exactly
                        # as it did before the net existed.
                        try:
                            pending = await _load_pending_restore(replica)
                            if pending:
                                preserve_payload, preserved = recover_pending_restore(
                                    preserve_payload, preserved, pending,
                                    await _master_config(), exclusions, replica.name,
                                )
                            if preserve_payload:
                                await _save_pending_restore(
                                    replica, preserve_payload, preserved,
                                )
                        except Exception as exc:
                            logger.warning(
                                "Could not persist the config snapshot for %s (%s) — "
                                "continuing; a failed restore will not be recoverable "
                                "by the next sync.", replica.name, exc,
                            )
                        logger.info(
                            "Preserving %d config key(s) across the import on %s: %s",
                            len(preserved), replica.name, ", ".join(preserved) or "none",
                        )

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

                    if preserve_payload and await _restore_preserved_config(
                        replica, preserve_payload, preserved,
                    ):
                        await _clear_pending_restore(replica)

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
                        preserved_keys=preserved,
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

        # Step 1 re-ran gravity on the master, which routinely shifts its
        # blocklist count by a few domains. Left alone, the next stats poll
        # reads that as a change and auto-gravity fires a second full sync
        # within the minute — a second import landing on replicas whose FTL
        # is still restarting from this one. Drop the watermark so that poll
        # re-baselines instead; this sync already pushed the fresh lists.
        _last_blocklist_by_site.pop(sid_key, None)

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


async def _patch_config_with_retry(replica: PiholeInstance, partial: dict) -> None:
    """PATCH a partial config onto a replica, waiting out an FTL restart.

    Only connection-level failures are retried: they mean FTL is still
    coming back up after the import, and the PATCH has not been judged yet.
    An HTTP error is FTL's answer and is raised at once, so the caller can
    fall back to key-by-key. The cached client is evicted between attempts —
    its keepalive socket died with the old FTL process.
    """
    delays = iter(_CONFIG_RESTORE_RETRY_DELAYS)
    while True:
        try:
            client = await get_client(replica)
            await client.patch_config(partial)
            await save_sid(replica.id, client.sid)
            return
        except _TRANSIENT_SYNC_ERRORS as exc:
            delay = next(delays, None)
            if delay is None:
                raise
            logger.warning(
                "Config restore on %s could not connect (%s: %s) — FTL is probably "
                "still restarting; retrying in %ss.",
                replica.name, type(exc).__name__, exc, delay,
            )
            await close_client(str(replica.id))
            await asyncio.sleep(delay)


async def _restore_preserved_config(
    replica: PiholeInstance, payload: dict, paths: list[str],
) -> bool:
    """Re-apply a replica's own values for the excluded keys after an import.

    The teleporter import has just replaced the whole of `pihole.toml`, so
    for a few seconds this replica is serving the master's copy of these
    keys. PATCH /api/config puts its own values back, touching nothing else.

    One PATCH carries every preserved key. If FTL rejects the batch — a
    single read-only or `FTLCONF_`-forced item is enough — we fall back to
    patching key by key so one bad key cannot cost the user the rest. A
    read-back then proves the values actually stuck; if they did not, this
    raises, because silently reporting success on a replica whose local DNS
    was just overwritten is the one outcome worse than a failed sync.

    Returns True only when the read-back proved the values stuck; the caller
    keeps the persisted snapshot around otherwise.
    """
    await asyncio.sleep(_CONFIG_RESTORE_SETTLE_SECONDS)

    try:
        await _patch_config_with_retry(replica, payload)
    except _TRANSIENT_SYNC_ERRORS as exc:
        # Unreachable for the whole retry window. Key-by-key would only sit
        # through the same window once per key; the persisted snapshot lets
        # the next sync finish the job instead.
        raise RuntimeError(
            "Keys marked keep-local were overwritten by the import and could "
            f"not be restored on {replica.name} ({', '.join(paths)}): the replica "
            f"stayed unreachable after the import ({exc}). Its own values are "
            "saved and the next sync will put them back."
        ) from exc
    except Exception as exc:
        logger.warning(
            "Batch config restore on %s failed (%s) — retrying key by key.",
            replica.name, exc,
        )
        failed: list[str] = []
        for path in paths:
            ok, value = _pluck(payload, path)
            if not ok:
                continue
            try:
                await _patch_config_with_retry(replica, _nest(path, value))
            except Exception as key_exc:
                logger.error(
                    "Could not restore config key '%s' on %s: %s",
                    path, replica.name, key_exc,
                )
                failed.append(path)
        if failed:
            raise RuntimeError(
                "Keys marked keep-local were overwritten by the import and could "
                f"not be restored on {replica.name}: {', '.join(failed)}"
            ) from exc

    # Read-back verification, in the same spirit as the DB settings writes:
    # a PATCH that returns 200 is not proof FTL kept the value.
    try:
        client = await get_client(replica)
        cfg_after = await client.get_config()
        await save_sid(replica.id, client.sid)
    except Exception as exc:
        logger.warning(
            "Could not read back preserved config from %s to verify it: %s",
            replica.name, exc,
        )
        return False

    drifted: list[str] = []
    for path in paths:
        want_ok, want = _pluck(payload, path)
        got_ok, got = _pluck(cfg_after, path)
        if want_ok and (not got_ok or got != want):
            drifted.append(path)
    if drifted:
        raise RuntimeError(
            f"Config restore on {replica.name} did not stick for: {', '.join(drifted)}"
        )
    logger.info(
        "Verified %d preserved config key(s) survived the import on %s.",
        len(paths), replica.name,
    )
    return True


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
